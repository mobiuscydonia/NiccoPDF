"""pdf_engine.py — headless PDF stamping engine for NiccoPDF.

All public coordinates are in *visual* PDF points: the coordinate system of the
page exactly as it is displayed/rendered (rotation and CropBox already applied),
origin at the top-left of the visible page.

Empirically verified conventions for pymupdf 1.28 (see tests/):
  - insert_text: wants derotated point + rotate=page.rotation. It correctly
    accounts for the CropBox on rotated pages.
  - insert_image / insert_htmlbox: want derotated rect + rotate=page.rotation,
    PLUS — on rotated pages only — a constant shift of
    (cropbox.x0 - mediabox.x0, -(mediabox.y1 - cropbox.y1)) in derotated space
    (they mis-handle offset CropBoxes when the page is rotated; verified across
    4 rotations x 4 crop configurations).
"""
from __future__ import annotations

import html
import io
import os
import re
import unicodedata
import warnings as _pywarnings
from dataclasses import dataclass, field, replace

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

# ---------------------------------------------------------------------------
# Fonts (per platform)
# ---------------------------------------------------------------------------

import sys as _sys

_IS_WIN = _sys.platform == "win32"
_IS_MAC = _sys.platform == "darwin"


def _first_existing(paths) -> str | None:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


if _IS_WIN:
    _WF = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    ARIAL_TTF = os.path.join(_WF, "arial.ttf")
    EMOJI_TTF = os.path.join(_WF, "seguiemj.ttf")   # Segoe UI Emoji (color)
    CJK_TTC = os.path.join(_WF, "msyh.ttc")         # Microsoft YaHei
    SYMBOL_TTF = os.path.join(_WF, "seguisym.ttf")  # Segoe UI Symbol
elif _IS_MAC:
    ARIAL_TTF = _first_existing([
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        os.path.expanduser("~/Library/Fonts/Arial.ttf"),
        "/System/Library/Fonts/Helvetica.ttc",
    ])
    # Apple Color Emoji uses fixed-size sbix strikes that Pillow cannot
    # render at arbitrary sizes — emoji text routes through insert_htmlbox
    # (clean monochrome glyphs) instead of the raster path on macOS.
    EMOJI_TTF = None
    CJK_TTC = _first_existing([
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ])
    SYMBOL_TTF = _first_existing(["/System/Library/Fonts/Apple Symbols.ttf"])
else:  # linux and friends
    ARIAL_TTF = _first_existing([
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])
    EMOJI_TTF = _first_existing([
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"])
    CJK_TTC = _first_existing([
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"])
    SYMBOL_TTF = None

_font_cache: dict[str, pymupdf.Font | None] = {}


def _helv_font() -> pymupdf.Font | None:
    if "<helv>" not in _font_cache:
        try:
            _font_cache["<helv>"] = pymupdf.Font("helv")
        except Exception:
            _font_cache["<helv>"] = None
    return _font_cache["<helv>"]


def _metric_font(path: str | None) -> pymupdf.Font | None:
    """A pymupdf Font for glyph-coverage queries and metrics (None path -> None)."""
    if path is None:
        return None
    if path not in _font_cache:
        try:
            _font_cache[path] = pymupdf.Font(fontfile=path) if os.path.exists(path) else None
        except Exception:
            _font_cache[path] = None
    return _font_cache[path]


def _primary_font() -> tuple[pymupdf.Font | None, str | None]:
    """(metric font, fontfile path or None-for-helv) for the main text font."""
    f = _metric_font(ARIAL_TTF)
    if f is not None:
        return f, ARIAL_TTF
    return _helv_font(), None


_EMOJI_RANGES = (
    (0x1F000, 0x1FBFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x2190, 0x21FF),
    (0xFE00, 0xFE0F),
    (0x1F1E6, 0x1F1FF),
    (0x200D, 0x200D),
    (0x2300, 0x23FF),
)

# joiners / modifiers that break apart without complex text shaping
_EMOJI_JOINERS = {0x200D, 0xFE0E, 0xFE0F}
_SKIN_TONES = set(range(0x1F3FB, 0x1F400))


def _has_emoji(text: str) -> bool:
    return any(a <= ord(c) <= b for c in text for a, b in _EMOJI_RANGES)


def _has_rtl(text: str) -> bool:
    return any(unicodedata.bidirectional(c) in ("R", "AL", "AN") for c in text)


def _covered(font: pymupdf.Font | None, text: str) -> bool:
    if font is None:
        return False
    try:
        return all(font.has_glyph(ord(c)) > 0 for c in text if not c.isspace())
    except Exception:
        return False


def _is_latin1(text: str) -> bool:
    return all(c == "\n" or 32 <= ord(c) <= 255 for c in text)


def sanitize_text(text: str) -> str:
    """Normalize line endings, drop control chars and lone surrogates."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif o < 32 or 0x7F <= o <= 0x9F:
            continue
        elif 0xD800 <= o <= 0xDFFF:
            out.append("\uFFFD")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Overlay model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextOverlay:
    """Text block. (x, y) = visual top-left of the block, points. size in pt."""
    page: int
    x: float
    y: float
    text: str
    size: float = 14.0
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ImageOverlay:
    """Image stamp. Rect in visual points. png = encoded PNG bytes (RGBA ok)."""
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    png: bytes = field(repr=False, default=b"")


Overlay = TextOverlay | ImageOverlay


def text_metrics(size: float) -> tuple[float, float]:
    """(ascender_pt, line_height_pt) for the primary text font at `size`."""
    f, _ = _primary_font()
    try:
        asc, desc = f.ascender, f.descender
    except Exception:
        asc, desc = 0.9, -0.21
    # sanity-clamp: some base fonts report bogus metrics (pymupdf's helv
    # claims ascender 1.07, which sinks the baseline visibly)
    if not (0.5 < asc <= 1.0):
        asc = 0.9
    if not (-0.6 < desc < 0.05):
        desc = -0.21
    return asc * size, (asc - desc) * size * 1.02


def measure_text(text: str, size: float) -> tuple[float, float]:
    """Approximate (width_pt, height_pt) of a text block in the primary font."""
    prim, _ = _primary_font()
    chain = [prim, _metric_font(CJK_TTC), _metric_font(EMOJI_TTF), _metric_font(SYMBOL_TTF)]
    _, lh = text_metrics(size)
    lines = text.split("\n")
    w = 0.0
    for line in lines:
        lw = 0.0
        for ch in line:
            adv = None
            for f in chain:
                if f is None:
                    continue
                try:
                    if f.has_glyph(ord(ch)) > 0:
                        adv = f.glyph_advance(ord(ch))
                        break
                except Exception:
                    continue
            if adv is None:
                adv = 0.6
            lw += adv * size
        w = max(w, lw)
    return w, lh * len(lines)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class EngineError(Exception):
    """User-presentable failure."""


_IMAGE_SIGS = (
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"RIFF", "webp"),
)


class Session:
    """An open source document. Read-only; edits are applied at export time.

    The whole file is read into memory at open time, so the source on disk can
    be moved, locked, modified or even deleted afterwards without affecting
    rendering or export — and exporting over the source path works.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        if not os.path.exists(self.path):
            raise EngineError(f"File not found:\n{self.path}")
        try:
            with open(self.path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise EngineError(
                "Could not read this file — it may be locked by another "
                f"program or you may not have permission.\n\n{exc}") from exc
        if not data:
            raise EngineError("This file is empty (0 bytes) — it is not a valid PDF.")
        self._data = data  # keep alive: pymupdf streams reference this buffer

        ext = os.path.splitext(self.path)[1].lower().lstrip(".")
        doc = None
        try:
            doc = pymupdf.open(stream=data, filetype=ext or "pdf")
        except Exception as first_exc:
            doc = self._reopen_by_content(data, first_exc)
        if not doc.is_pdf:
            # Image (or epub/xps/...) source: convert to a PDF so the rest of
            # the pipeline is uniform.
            try:
                pdfbytes = doc.convert_to_pdf()
                doc.close()
                self._data = pdfbytes
                doc = pymupdf.open("pdf", pdfbytes)
            except Exception as exc:
                raise EngineError(f"Could not convert this file to a PDF:\n{exc}") from exc
        self.doc = doc
        self.needs_password = bool(doc.needs_pass)
        if not self.needs_password:
            self._normalize_rotations()

    def _reopen_by_content(self, data: bytes, orig_exc: Exception) -> pymupdf.Document:
        head = data[:1024]
        for sig, kind in _IMAGE_SIGS:
            if head.startswith(sig):
                try:
                    return pymupdf.open(stream=data, filetype=kind)
                except Exception:
                    break
        if b"%PDF" in head:
            try:
                return pymupdf.open(stream=data, filetype="pdf")
            except Exception:
                pass
        raise EngineError(
            "This file could not be opened as a PDF (it may be corrupt or not "
            f"actually a PDF).\n\nDetails: {orig_exc}") from orig_exc

    def _raw_rotate(self, xref: int) -> float | None:
        """Resolve the effective raw /Rotate for a page object: direct value,
        indirect reference, or inherited from the /Pages parent chain."""
        seen: set[int] = set()
        x = xref
        while x and x not in seen and len(seen) < 64:
            seen.add(x)
            try:
                t, v = self.doc.xref_get_key(x, "Rotate")
            except Exception:
                return None
            if t in ("int", "float", "real"):
                try:
                    return float(v)
                except Exception:
                    return None
            if t == "xref":
                try:
                    n = int(v.split()[0])
                    src = self.doc.xref_object(n).strip()
                    return float(src)
                except Exception:
                    return None
            try:
                tp, vp = self.doc.xref_get_key(x, "Parent")
            except Exception:
                return None
            if tp == "xref":
                try:
                    x = int(vp.split()[0])
                    continue
                except Exception:
                    return None
            return None
        return None

    def _normalize_rotations(self) -> None:
        """Snap spec-invalid /Rotate values (45, 45.0, inherited or indirect)
        to a multiple of 90 so rendering and export agree with each other."""
        try:
            if not self.doc.is_pdf:
                return
            for i in range(self.doc.page_count):
                try:
                    page = self.doc[i]
                    r = self._raw_rotate(page.xref)
                    if r is not None and r % 90 != 0:
                        page.set_rotation(int(((r % 360) / 90.0) + 0.5) * 90 % 360)
                except Exception:
                    continue
        except Exception:
            pass

    # -- password ----------------------------------------------------------
    def authenticate(self, password: str) -> bool:
        ok = bool(self.doc.authenticate(password or ""))
        if ok:
            self.needs_password = False
            self._normalize_rotations()
        return ok

    # -- info --------------------------------------------------------------
    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def is_form(self) -> bool:
        try:
            return bool(self.doc.is_form_pdf)
        except Exception:
            return False

    def page_size(self, pno: int) -> tuple[float, float]:
        try:
            r = self.doc[pno].rect
        except Exception as exc:
            raise EngineError(f"Page {pno + 1} of this PDF is unreadable:\n{exc}") from exc
        return r.width, r.height

    # -- rendering ---------------------------------------------------------
    def render_page(self, pno: int, zoom: float,
                    max_pixels: float = 60_000_000) -> tuple[Image.Image, float]:
        """Render page `pno` at `zoom`. Returns (RGB image, effective zoom)."""
        try:
            page = self.doc[pno]
            w, h = page.rect.width, page.rect.height
            if w <= 0 or h <= 0:
                raise EngineError(f"Page {pno + 1} has an invalid size.")
            z = max(zoom, 0.02)
            if w * z * h * z > max_pixels:
                z = (max_pixels / (w * h)) ** 0.5
            pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            return img, z
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(f"Page {pno + 1} of this PDF could not be "
                              f"rendered:\n{exc}") from exc

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _page_rot(page: pymupdf.Page) -> int:
    try:
        rot = int(page.rotation) % 360
    except Exception:
        rot = 0
    return rot if rot in (0, 90, 180, 270) else 0


def _derot_point(page: pymupdf.Page, p: pymupdf.Point) -> pymupdf.Point:
    return pymupdf.Point(p) * page.derotation_matrix


_calib_cache: dict[tuple, tuple[float, float]] = {}
_CALIB_SQUARE: bytes | None = None


def _calib_square() -> bytes:
    global _CALIB_SQUARE
    if _CALIB_SQUARE is None:
        img = Image.new("RGB", (24, 24), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        _CALIB_SQUARE = buf.getvalue()
    return _CALIB_SQUARE


def _rect_insert_shift(page: pymupdf.Page) -> tuple[float, float]:
    """Measured visual-space displacement that insert_image/insert_htmlbox
    introduce on this page's geometry (rotation + MediaBox/CropBox origins).

    MuPDF places rect-based insertions in a frame whose origin drifts from the
    rendered (visual) frame on rotated pages with non-trivial page boxes, and
    the drift is a pure translation per geometry. Rather than trusting any
    closed-form formula (two attempts failed on exotic box combinations), we
    CALIBRATE: replicate the page's boxes+rotation on a blank in-memory page,
    insert a probe square at its visual center the naive way, render, and
    measure where it actually landed. Cached per unique geometry.
    """
    rot = _page_rot(page)
    if rot == 0:
        return (0.0, 0.0)
    try:
        mb = pymupdf.Rect(page.mediabox)
        cbp = pymupdf.Rect(page.cropbox)  # pymupdf's top-down representation
        # reconstruct the raw PDF CropBox from pymupdf's flipped property
        raw_cb = (cbp.x0, mb.y1 - cbp.y1, cbp.x1, mb.y1 - cbp.y0)
        key = (round(mb.x0, 2), round(mb.y0, 2), round(mb.x1, 2), round(mb.y1, 2),
               tuple(round(v, 2) for v in raw_cb), rot)
        if key in _calib_cache:
            return _calib_cache[key]

        d = pymupdf.open()
        d.new_page(width=max(mb.width, 8), height=max(mb.height, 8))
        px = d[0].xref
        d.xref_set_key(px, "MediaBox",
                       f"[{mb.x0:g} {mb.y0:g} {mb.x1:g} {mb.y1:g}]")
        d.xref_set_key(px, "CropBox",
                       f"[{raw_cb[0]:g} {raw_cb[1]:g} {raw_cb[2]:g} {raw_cb[3]:g}]")
        d.xref_set_key(px, "Rotate", str(rot))
        replica = d.tobytes()
        d.close()

        def probe(vis_shift: tuple[float, float], full: bool):
            """Insert a probe (full-page or centered square), pre-shifted by
            -vis_shift in visual space; return the dark bbox+centroid in pt."""
            dd = pymupdf.open("pdf", replica)
            pp = dd[0]
            pr = pp.rect
            if full:
                tgt = pymupdf.Rect(0, 0, pr.width, pr.height)
            else:
                s = max(8.0, min(pr.width, pr.height) * 0.12)
                tgt = pymupdf.Rect(pr.width / 2 - s / 2, pr.height / 2 - s / 2,
                                   pr.width / 2 + s / 2, pr.height / 2 + s / 2)
            moved = pymupdf.Rect(tgt.x0 - vis_shift[0], tgt.y0 - vis_shift[1],
                                 tgt.x1 - vis_shift[0], tgt.y1 - vis_shift[1])
            nr = moved * pp.derotation_matrix
            nr.normalize()
            pp.insert_image(nr, stream=_calib_square(), rotate=rot,
                            keep_proportion=False)
            z = 560.0 / max(pr.width, pr.height, 1)
            pix = pp.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
            xs = ys = n = 0
            bx0 = by0 = 10 ** 9
            bx1 = by1 = -1
            for x in range(pix.width):
                for y in range(pix.height):
                    if sum(pix.pixel(x, y)[:3]) < 150:
                        xs += x
                        ys += y
                        n += 1
                        bx0 = min(bx0, x)
                        by0 = min(by0, y)
                        bx1 = max(bx1, x)
                        by1 = max(by1, y)
            W, H = pr.width, pr.height
            dd.close()
            if n == 0:
                return None
            return {"c": (xs / n / z, ys / n / z),
                    "bbox": (bx0 / z, by0 / z, (bx1 + 1) / z, (by1 + 1) / z),
                    "W": W, "H": H, "tgt": tgt}

        # Pass 1: full-page probe — recover the coarse translation from which
        # edges of the page stayed white (works for arbitrarily large shifts).
        r1 = probe((0.0, 0.0), full=True)
        if r1 is None:
            shift = (0.0, 0.0)  # give up; export verification will warn
        else:
            bx0, by0, bx1, by1 = r1["bbox"]
            W, H = r1["W"], r1["H"]
            dx = bx0 if bx0 > 1.5 else (bx1 - W if bx1 < W - 1.5 else 0.0)
            dy = by0 if by0 > 1.5 else (by1 - H if by1 < H - 1.5 else 0.0)
            # Pass 2: centered probe with the coarse correction; refine with
            # the measured residual.
            r2p = probe((dx, dy), full=False)
            if r2p is not None:
                tc = ((r2p["tgt"].x0 + r2p["tgt"].x1) / 2,
                      (r2p["tgt"].y0 + r2p["tgt"].y1) / 2)
                dx += r2p["c"][0] - tc[0]
                dy += r2p["c"][1] - tc[1]
            shift = (dx, dy)
        if abs(shift[0]) < 0.75 and abs(shift[1]) < 0.75:
            shift = (0.0, 0.0)
        _calib_cache[key] = shift
        return shift
    except Exception:
        return (0.0, 0.0)


def _derot_rect_boxfix(page: pymupdf.Page, r: pymupdf.Rect) -> pymupdf.Rect:
    """Visual rect -> the rect insert_image/insert_htmlbox need, compensating
    MuPDF's placement drift via per-geometry calibration (_rect_insert_shift).
    """
    dx, dy = _rect_insert_shift(page)
    rr = pymupdf.Rect(r.x0 - dx, r.y0 - dy, r.x1 - dx, r.y1 - dy)
    r2 = rr * page.derotation_matrix
    r2.normalize()
    return r2


# ---------------------------------------------------------------------------
# Text insertion strategies
# ---------------------------------------------------------------------------


def _insert_text_vector(page: pymupdf.Page, ov: TextOverlay) -> None:
    """Base-14 Helvetica insertion (Latin-1 text): crisp, searchable, and its
    extracted text has real spaces. Baseline offset uses the SAME metrics as
    the GUI preview font (Arial) so all engine paths and the canvas agree."""
    rot = _page_rot(page)
    kwargs: dict = {"fontsize": ov.size, "color": ov.color, "rotate": rot,
                    "fontname": "helv"}
    asc, lh = text_metrics(ov.size)
    for i, line in enumerate(ov.text.split("\n")):
        if not line.strip():
            continue
        vp = pymupdf.Point(ov.x, ov.y + asc + i * lh)
        page.insert_text(_derot_point(page, vp), line, **kwargs)


def _spaces_to_nbsp(escaped_line: str) -> str:
    """Preserve leading spaces and runs of spaces in HTML."""
    def runs(m):
        n = len(m.group(0))
        return "&nbsp;" * (n - 1) + " "
    s = re.sub(r" {2,}", runs, escaped_line)
    if s.startswith(" "):
        s = "&nbsp;" + s[1:]
    return s


def _insert_text_htmlbox(page: pymupdf.Page, ov: TextOverlay,
                         warnings: list[str]) -> None:
    w, h = measure_text(ov.text, ov.size)
    rect = pymupdf.Rect(ov.x, ov.y, ov.x + w * 1.45 + 2 * ov.size + 8,
                        ov.y + h * 1.35 + ov.size + 8)
    r, g, b = (max(0, min(255, int(round(c * 255)))) for c in ov.color)
    css = ("* {{ font-family: sans-serif; font-size: {s}pt; color: #{r:02x}{g:02x}{b:02x}; "
           "margin: 0; padding: 0; line-height: 1.14; }}").format(s=ov.size, r=r, g=g, b=b)
    body = "<br>".join(
        _spaces_to_nbsp(html.escape(line)) if line.strip() else "&nbsp;"
        for line in ov.text.split("\n"))
    rot = _page_rot(page)
    res = page.insert_htmlbox(_derot_rect_boxfix(page, rect), body, css=css, rotate=rot)
    try:
        scale = res[1]
        if scale not in (None, 0) and scale < 0.99:
            warnings.append(
                f"Page {ov.page + 1}: a text box was scaled to {scale:.0%} to fit.")
    except Exception:
        pass


def _pil_font(path: str, px: int, cache: dict) -> ImageFont.FreeTypeFont | None:
    key = (path, px)
    if key not in cache:
        try:
            cache[key] = ImageFont.truetype(path, px) if os.path.exists(path) else None
        except Exception:
            cache[key] = None
    return cache[key]


def _insert_text_raster(page: pymupdf.Page, ov: TextOverlay,
                        warnings: list[str]) -> None:
    """Rasterize text with Windows fonts (color emoji capable), stamp as image.

    Only used for LTR text containing emoji. Without a complex-text-shaping
    engine, ZWJ sequences and skin-tone modifiers render as broken artifact
    glyphs — strip them so composite emoji degrade to their clean components.
    """
    text = "".join(
        ch for ch in ov.text
        if ord(ch) not in _EMOJI_JOINERS and ord(ch) not in _SKIN_TONES)
    SCALE = 4
    px = max(4, int(round(ov.size * SCALE)))
    chain = [
        (ARIAL_TTF, _metric_font(ARIAL_TTF)),
        (EMOJI_TTF, _metric_font(EMOJI_TTF)),
        (CJK_TTC, _metric_font(CJK_TTC)),
        (SYMBOL_TTF, _metric_font(SYMBOL_TTF)),
    ]
    chain = [(p, f) for p, f in chain if f is not None]
    if not chain:
        _insert_text_htmlbox(page, ov, warnings)
        return
    pil_cache: dict = {}

    def pick(ch: str) -> str:
        for p, f in chain:
            try:
                if f.has_glyph(ord(ch)) > 0:
                    return p
            except Exception:
                continue
        return chain[0][0]

    lines = text.split("\n")
    asc_pt, lh_pt = text_metrics(ov.size)
    lh_px = int(round(lh_pt * SCALE))
    asc_px = int(round(asc_pt * SCALE))

    runs_per_line: list[list[tuple[str, str]]] = []
    widths = []
    dummy = Image.new("RGBA", (8, 8))
    dd = ImageDraw.Draw(dummy)
    for line in lines:
        runs: list[tuple[str, str]] = []
        for ch in line:
            fp = pick(ch)
            if runs and runs[-1][0] == fp:
                runs[-1] = (fp, runs[-1][1] + ch)
            else:
                runs.append((fp, ch))
        runs_per_line.append(runs)
        wpx = 0.0
        for fp, run in runs:
            fnt = _pil_font(fp, px, pil_cache)
            if fnt is not None:
                try:
                    wpx += dd.textlength(run, font=fnt)
                except Exception:
                    wpx += len(run) * px * 0.6
        widths.append(wpx)

    W = max(4, int(max(widths) + px * 0.3))
    H = max(4, lh_px * len(lines))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r, g, b = (max(0, min(255, int(round(c * 255)))) for c in ov.color)
    for i, runs in enumerate(runs_per_line):
        x = 0.0
        base = i * lh_px + asc_px
        for fp, run in runs:
            fnt = _pil_font(fp, px, pil_cache)
            if fnt is None:
                continue
            try:
                draw.text((x, base), run, font=fnt, fill=(r, g, b, 255),
                          anchor="ls", embedded_color=True)
                x += draw.textlength(run, font=fnt)
            except Exception:
                pass
    buf = io.BytesIO()
    img.save(buf, "PNG")
    stamp = ImageOverlay(page=ov.page, x0=ov.x, y0=ov.y,
                         x1=ov.x + W / SCALE, y1=ov.y + H / SCALE,
                         png=buf.getvalue())
    _insert_image(page, stamp, warnings)


def _insert_text(page: pymupdf.Page, ov: TextOverlay, warnings: list[str]) -> None:
    clean = sanitize_text(ov.text)
    if not clean.strip():
        return
    if clean != ov.text:
        ov = replace(ov, text=clean)
    # warn about content that will be clipped by the page edge
    try:
        w, h = measure_text(clean, ov.size)
        pr = page.rect
        if ov.y + h > pr.height + 2 or ov.x + w > pr.width + 2 or ov.x < -2 or ov.y < -2:
            warnings.append(
                f"Page {ov.page + 1}: a text box extends beyond the page "
                "edge; the overflow will be cut off.")
    except Exception:
        pass
    if _has_rtl(clean):
        # Story engine does proper bidi + shaping (emoji render monochrome).
        _insert_text_htmlbox(page, ov, warnings)
    elif _has_emoji(clean) and _metric_font(EMOJI_TTF) is not None:
        _insert_text_raster(page, ov, warnings)
    elif _has_emoji(clean):
        # no color-emoji-capable raster font on this platform (macOS):
        # Story/htmlbox renders emoji as clean monochrome glyphs
        _insert_text_htmlbox(page, ov, warnings)
    elif _is_latin1(clean):
        # base-14 Helvetica: metrically Arial-like and cleanly text-extractable
        _insert_text_vector(page, ov)
    else:
        # Story/htmlbox for everything else (Cyrillic, Greek, CJK, ...): its
        # embedded fonts produce clean, spec-valid ToUnicode maps, unlike
        # MuPDF's embedding of a raw TTF (NBSP-for-space, odd bfranges).
        _insert_text_htmlbox(page, ov, warnings)


# ---------------------------------------------------------------------------
# Image insertion
# ---------------------------------------------------------------------------


def _insert_image(page: pymupdf.Page, ov: ImageOverlay, warnings: list[str]) -> None:
    rect = pymupdf.Rect(ov.x0, ov.y0, ov.x1, ov.y1)
    rect.normalize()
    if rect.width < 0.5 or rect.height < 0.5:
        warnings.append(f"Page {ov.page + 1}: an image was too small to place; skipped.")
        return
    if not ov.png:
        warnings.append(f"Page {ov.page + 1}: an image had no data; skipped.")
        return
    rot = _page_rot(page)
    page.insert_image(_derot_rect_boxfix(page, rect), stream=ov.png,
                      rotate=rot, keep_proportion=False, overlay=True)


def prepare_image(path_or_bytes, white_to_alpha: bool = False,
                  max_dim: int = 3000) -> tuple[bytes, int, int]:
    """Load any raster image; returns (png_bytes_rgba, width_px, height_px).

    Honors EXIF orientation, handles 16/32-bit and float modes, downscales
    huge images, and optionally strips a white scan background.
    """
    old_limit = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 300_000_000
        with _pywarnings.catch_warnings():
            _pywarnings.simplefilter("ignore", Image.DecompressionBombWarning)
            try:
                if isinstance(path_or_bytes, (bytes, bytearray)):
                    img = Image.open(io.BytesIO(path_or_bytes))
                else:
                    img = Image.open(path_or_bytes)
                # explicit cap BEFORE decoding (MAX_IMAGE_PIXELS alone only
                # hard-errors at 2x its value, and full decode of a huge image
                # would spike gigabytes of RAM)
                if img.width * img.height > 250_000_000:
                    raise EngineError(
                        "That image has too many pixels to use safely "
                        f"({img.width}x{img.height}, over 250 megapixels). "
                        "Please downscale it first.")
                if img.format == "JPEG" and max(img.size) > 2 * max_dim:
                    img.draft("RGB", (2 * max_dim, 2 * max_dim))
                img.load()
                if getattr(img, "n_frames", 1) > 1:
                    img.seek(0)
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                img = _to_rgba(img)
            except Image.DecompressionBombError as exc:
                raise EngineError(
                    "That image has too many pixels to use safely "
                    "(over 250 megapixels). Please downscale it first.") from exc
            except EngineError:
                raise
            except Exception as exc:
                raise EngineError(f"Could not read that image:\n{exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit
    if img.width < 1 or img.height < 1:
        raise EngineError("That image is empty.")
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        img = img.resize((max(1, int(img.width * ratio)),
                          max(1, int(img.height * ratio))),
                         Image.Resampling.LANCZOS)
    if white_to_alpha:
        img = strip_white_background(img)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), img.width, img.height


def _to_rgba(img: Image.Image) -> Image.Image:
    """convert('RGBA') that doesn't clip high-bit-depth grayscale to white.

    Values are scaled by the mode's NOMINAL range (16-bit: /65535, float: 0-1),
    so uniform/dark images keep their actual brightness instead of being
    min-max stretched or blanked.
    """
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I", "F"):
        base = img.convert("F")
        lo, hi = base.getextrema()
        if img.mode == "F" and -0.001 <= lo and hi <= 1.001:
            scale, off = 255.0, 0.0
        elif img.mode == "F" and 0 <= lo and hi <= 255.5:
            scale, off = 1.0, 0.0
        elif 0 <= lo and hi <= 65535.5:
            scale, off = 255.0 / 65535.0, 0.0
        elif hi > lo:  # arbitrary range: min-max as a last resort
            scale, off = 255.0 / (hi - lo), lo
        else:          # uniform with out-of-range value: clamp it
            v = int(min(255.0, max(0.0, lo)))
            return Image.new("L", img.size, v).convert("RGBA")
        base = base.point(lambda v: (v - off) * scale)
        img = base.convert("L")
    return img.convert("RGBA")


def strip_white_background(img: Image.Image) -> Image.Image:
    """Turn near-white background transparent (for scanned signatures)."""
    img = img.convert("RGBA")
    gray = img.convert("L")
    # lum >= 242 -> fully transparent; <= 190 -> opaque; ramp between.
    computed = gray.point(
        lambda v: 0 if v >= 242 else (255 if v <= 190 else int(255 * (242 - v) / 52)))
    alpha = ImageChops.multiply(img.getchannel("A"), computed)
    img.putalpha(alpha)
    return img


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _overlay_rect(ov: Overlay) -> pymupdf.Rect:
    if isinstance(ov, ImageOverlay):
        r = pymupdf.Rect(ov.x0, ov.y0, ov.x1, ov.y1)
        r.normalize()
        return r
    w, h = measure_text(ov.text, ov.size)
    return pymupdf.Rect(ov.x, ov.y, ov.x + max(w, 8), ov.y + max(h, ov.size))


def _verify_output(session: Session, out_path: str, overlays: list[Overlay],
                   warnings: list[str]) -> None:
    """Safety net: compare source vs output renders so silent stamp loss or
    page corruption is reported instead of shipped unnoticed."""
    try:
        out_doc = pymupdf.open(out_path)
    except Exception:
        warnings.append("The saved file could not be re-opened for verification.")
        return
    try:
        by_page: dict[int, list[Overlay]] = {}
        for ov in overlays:
            if 0 <= ov.page < out_doc.page_count and ov.page < session.page_count:
                by_page.setdefault(ov.page, []).append(ov)
        checks = 0
        for pno, ovs in sorted(by_page.items()):
            if checks >= 24:
                break
            try:
                src_page = session.doc[pno]
                out_page = out_doc[pno]
                page_rect = src_page.rect
                # 1) whole-page ink sanity (downsampled)
                z = min(0.35, 600.0 / max(page_rect.width, page_rect.height, 1))
                m = pymupdf.Matrix(z, z)
                sp = src_page.get_pixmap(matrix=m, alpha=False)
                op = out_page.get_pixmap(matrix=m, alpha=False)
                src_ink = _ink(sp)
                out_ink = _ink(op)
                if src_ink > 200 and out_ink < 0.15 * src_ink:
                    warnings.append(
                        f"Page {pno + 1}: the saved page lost most of its "
                        "content — the source PDF may be too damaged to edit "
                        "safely. Check the output before sending it.")
                    checks += 1
                    continue
                # 2) per-stamp region: output must differ from source
                for ov in ovs[:8]:
                    checks += 1
                    r = _overlay_rect(ov) & page_rect
                    if r.is_empty or r.width < 1 or r.height < 1:
                        kind = ("text" if isinstance(ov, TextOverlay) else "image")
                        warnings.append(
                            f"Page {pno + 1}: a {kind} item lies outside the "
                            "visible page area and will not be seen.")
                        continue
                    zz = min(1.5, 400.0 / max(r.width, r.height, 1))
                    mm = pymupdf.Matrix(zz, zz)
                    a = src_page.get_pixmap(matrix=mm, clip=r, alpha=False)
                    b = out_page.get_pixmap(matrix=mm, clip=r, alpha=False)
                    if a.width != b.width or a.height != b.height:
                        continue
                    if _diff_pixels(a, b) < 3:
                        kind = ("text" if isinstance(ov, TextOverlay) else "image")
                        warnings.append(
                            f"Page {pno + 1}: a {kind} item does not appear "
                            "to be visible in the saved file.")
            except Exception:
                continue
    finally:
        try:
            out_doc.close()
        except Exception:
            pass


def _ink(pix) -> int:
    n = 0
    for x in range(0, pix.width, 2):
        for y in range(0, pix.height, 2):
            if sum(pix.pixel(x, y)[:3]) < 690:
                n += 1
    return n


def _diff_pixels(a, b) -> int:
    n = 0
    for x in range(0, a.width, 2):
        for y in range(0, a.height, 2):
            pa = a.pixel(x, y)
            pb = b.pixel(x, y)
            if (abs(pa[0] - pb[0]) > 8 or abs(pa[1] - pb[1]) > 8
                    or abs(pa[2] - pb[2]) > 8):
                n += 1
                if n >= 3:
                    return n
    return n


def export(session: Session, out_path: str, overlays: list[Overlay],
           flatten_forms: bool = True, verify: bool = True) -> list[str]:
    """Apply overlays to a decrypted copy of the source and save to out_path.

    Returns a list of human-readable warnings (empty = clean).
    """
    if session.needs_password:
        raise EngineError("The document is locked — enter its password first.")
    warnings: list[str] = []

    if "\x00" in out_path:
        raise EngineError("The save path contains an invalid character.")
    out_path = os.path.abspath(out_path)
    if os.path.isdir(out_path):
        raise EngineError(f"The save path is a folder, not a file:\n{out_path}")
    parent = os.path.dirname(out_path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise EngineError(f"The folder for the save path could not be "
                          f"created:\n{parent}\n\n{exc}") from exc

    # 1. Decrypted in-memory working copy (never touches the source file).
    work = None
    try:
        repaired = False
        try:
            repaired = bool(session.doc.is_repaired)
        except Exception:
            pass
        if repaired:
            # rebuild + sanitize content streams: a repaired (e.g. truncated)
            # file otherwise yields pages that silently drop new content
            data = session.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE,
                                       garbage=4, clean=True)
        else:
            data = session.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
        work = pymupdf.open("pdf", data)
    except Exception:
        try:
            work = pymupdf.open()
            work.insert_pdf(session.doc)
            warnings.append("The PDF had structural damage; it was rebuilt "
                            "(some interactive features may be gone).")
        except Exception as exc:
            raise EngineError(f"Could not process this PDF:\n{exc}") from exc

    try:
        # 2. Flatten form fields so stamped output looks identical everywhere.
        if flatten_forms:
            try:
                if work.is_form_pdf:
                    work.bake(annots=False, widgets=True)
            except Exception as exc:
                warnings.append(f"Form fields could not be flattened ({exc}); "
                                "they were left interactive.")

        # 3. Apply overlays.
        for ov in overlays:
            if ov.page < 0 or ov.page >= work.page_count:
                warnings.append(f"An item targeted missing page {ov.page + 1}; skipped.")
                continue
            page = work[ov.page]
            try:
                if isinstance(ov, TextOverlay):
                    _insert_text(page, ov, warnings)
                else:
                    _insert_image(page, ov, warnings)
            except Exception as exc:
                kind = "text" if isinstance(ov, TextOverlay) else "image"
                warnings.append(f"Page {ov.page + 1}: could not place a {kind} "
                                f"item ({exc}).")

        # 4. Shrink embedded fonts, save.
        try:
            work.subset_fonts()
        except Exception:
            pass
        try:
            work.save(out_path, garbage=3, deflate=True,
                      encryption=pymupdf.PDF_ENCRYPT_NONE)
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(f"Could not write the PDF to\n{out_path}\n\n{exc}") from exc
    finally:
        try:
            work.close()
        except Exception:
            pass

    # 5. Verify what actually got written.
    if verify and overlays:
        _verify_output(session, out_path, overlays, warnings)
    return warnings
