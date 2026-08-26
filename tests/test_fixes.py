"""Regression tests for every bug confirmed by the adversarial round.

Run:  python tests/test_fixes.py     (exit 0 = all pass)
"""
import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pymupdf
from PIL import Image

import pdf_engine as eng

CORPUS = os.path.join(HERE, "corpus")
OUT = os.path.join(HERE, "out_fixes")
os.makedirs(OUT, exist_ok=True)

fails = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        fails.append(name)
    print(f"{status}  {name}  {detail}")


def magenta_png(w=60, h=60):
    img = Image.new("RGBA", (w, h), (255, 0, 255, 255))
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def count_px(pix, pred, box=None):
    x0, y0, x1, y1 = box or (0, 0, pix.width, pix.height)
    n = 0
    for x in range(max(0, int(x0)), min(pix.width, int(x1)), 2):
        for y in range(max(0, int(y0)), min(pix.height, int(y1)), 2):
            if pred(pix.pixel(x, y)):
                n += 1
    return n


def is_mag(px):
    return px[0] > 170 and px[2] > 170 and px[1] < 110


def centroid_mag(pix):
    xs = ys = n = 0
    for x in range(0, pix.width, 2):
        for y in range(0, pix.height, 2):
            if is_mag(pix.pixel(x, y)):
                xs += x; ys += y; n += 1
    return (xs / n, ys / n) if n else None


# ---------------------------------------------------------------- BUG A: rot+crop
def bug_a():
    crops = [(30, 50, 500, 780), (72, 100, 540, 700), (100, 0, 612, 500)]
    for crop in crops:
        for rot in (0, 90, 180, 270):
            doc = pymupdf.open()
            page = doc.new_page(width=612, height=792)
            page.set_cropbox(pymupdf.Rect(*crop))
            page.set_rotation(rot)
            src = os.path.join(OUT, f"a_src_{rot}_{crop[0]}_{crop[1]}.pdf")
            doc.save(src)
            doc.close()
            s = eng.Session(src)
            pw, ph = s.page_size(0)
            tx, ty = pw * 0.5, ph * 0.4
            ovs = [eng.ImageOverlay(0, tx - 20, ty - 20, tx + 20, ty + 20,
                                    png=magenta_png()),
                   eng.TextOverlay(0, pw * 0.1, ph * 0.1, "Sig here", 12,
                                   (0.85, 0, 0))]
            outp = os.path.join(OUT, f"a_out_{rot}_{crop[0]}_{crop[1]}.pdf")
            warn = eng.export(s, outp, ovs)
            s.close()
            d2 = pymupdf.open(outp)
            pix = d2[0].get_pixmap()
            c = centroid_mag(pix)
            ok = c is not None and abs(c[0] - tx) < 5 and abs(c[1] - ty) < 5
            red = count_px(pix, lambda p: p[0] > 140 and p[1] < 90 and p[2] < 90,
                           (pw * 0.1 - 3, ph * 0.1 - 3, pw * 0.1 + 90, ph * 0.1 + 30))
            d2.close()
            check(f"A rot={rot} crop={crop}", ok and red >= 2,
                  f"centroid={c} want=({tx:.0f},{ty:.0f}) red={red} warn={warn}")


# ------------------------------------------------- BUG B: 16-bit grayscale scans
def bug_b():
    for fmt, name in (("PNG", "16png"), ("TIFF", "16tiff")):
        img = Image.new("I;16", (200, 100), 60000)  # near-white paper
        for x in range(20, 180):
            for y in range(40, 60):
                img.putpixel((x, y), 3000)  # dark signature stroke
        p = os.path.join(OUT, f"sig_{name}.{fmt.lower()}")
        img.save(p, fmt)
        png, w, h = eng.prepare_image(p)
        out = Image.open(io.BytesIO(png)).convert("RGB")
        dark = sum(1 for x in range(0, w, 2) for y in range(0, h, 2)
                   if sum(out.getpixel((x, y))) < 250)
        check(f"B 16-bit {fmt}", dark > 100, f"dark_px={dark}")
        # with white_to_alpha the stroke must survive as opaque
        png2, _, _ = eng.prepare_image(p, white_to_alpha=True)
        out2 = Image.open(io.BytesIO(png2))
        op = sum(1 for x in range(0, w, 2) for y in range(0, h, 2)
                 if out2.getpixel((x, y))[3] > 200)
        check(f"B 16-bit {fmt} +white_to_alpha", op > 100, f"opaque_px={op}")
    # uniform white 16-bit: must not crash, must not go black
    img = Image.new("I;16", (50, 50), 65535)
    p = os.path.join(OUT, "uniform16.png")
    img.save(p, "PNG")
    png, _, _ = eng.prepare_image(p)
    out = Image.open(io.BytesIO(png)).convert("L")
    check("B uniform white 16-bit", out.getpixel((25, 25)) > 200,
          f"px={out.getpixel((25, 25))}")


# ---------------------------------------------------- BUG C: EXIF orientation
def bug_c():
    import PIL.Image
    base = Image.new("RGB", (400, 200), (255, 255, 255))
    for x in range(0, 200):
        for y in range(0, 200):
            base.putpixel((x, y), (10, 10, 10))  # dark LEFT half (landscape)
    p = os.path.join(OUT, "exif6.jpg")
    exif = base.getexif()
    exif[274] = 6  # rotate 90 CW on view -> portrait, dark part at TOP
    base.save(p, "JPEG", exif=exif)
    png, w, h = eng.prepare_image(p)
    check("C EXIF portrait dims", h > w, f"{w}x{h}")
    out = Image.open(io.BytesIO(png)).convert("L")
    top = sum(out.getpixel((w // 2, y)) for y in range(0, h // 4))
    bot = sum(out.getpixel((w // 2, y)) for y in range(3 * h // 4, h))
    check("C EXIF rotation applied", top < bot, f"top={top} bot={bot}")


# ------------------------------------- BUG D: RTL with emoji, ZWJ artifacts
def bug_d():
    src = os.path.join(CORPUS, "normal_letter.pdf")
    cases = [("rtl_mix", "אושר ✓ מרחבא"),
             ("zwj_family", "OK \U0001F468\u200D\U0001F469\u200D\U0001F467 done"),
             ("skin", "\U0001F44D\U0001F3FD yes")]
    for name, txt in cases:
        s = eng.Session(src)
        outp = os.path.join(OUT, f"d_{name}.pdf")
        warn = eng.export(s, outp, [eng.TextOverlay(0, 60, 300, txt, 16, (0, 0, 0))])
        s.close()
        d = pymupdf.open(outp)
        pix = d[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
        ink = count_px(pix, lambda p: sum(p[:3]) < 700, (110, 590, 1000, 700))
        d.close()
        check(f"D {name} renders", ink > 10, f"ink={ink} warn={warn}")


# -------------------------------- BUG E: corrupt/repaired sources, verification
def bug_e():
    src = os.path.join(CORPUS, "corrupt_truncated.pdf")
    # image-ONLY stamp (the order-dependent silent-loss case)
    s = eng.Session(src)
    outp = os.path.join(OUT, "e_corrupt_imgonly.pdf")
    warn = eng.export(s, outp, [eng.ImageOverlay(0, 200, 300, 300, 400,
                                                 png=magenta_png())])
    s.close()
    d = pymupdf.open(outp)
    pix = d[0].get_pixmap()
    mag = count_px(pix, is_mag, (202, 302, 298, 398))
    d.close()
    check("E corrupt img-only stamp visible", mag > 100, f"magenta={mag} warn={warn}")

    # verification net: a deliberately-missing stamp must produce a warning
    s = eng.Session(os.path.join(CORPUS, "normal_letter.pdf"))
    outp2 = os.path.join(OUT, "e_offpage.pdf")
    warn2 = eng.export(s, outp2, [eng.ImageOverlay(0, -500, -500, -400, -400,
                                                   png=magenta_png())])
    s.close()
    check("E off-page stamp warned or skipped", len(warn2) >= 1, f"warn={warn2}")


# ------------------------------------------- BUG I/K: save-over-source, helv text
def bug_ik():
    import shutil
    src = os.path.join(OUT, "inplace.pdf")
    shutil.copy(os.path.join(CORPUS, "normal_letter.pdf"), src)
    s = eng.Session(src)
    warn = eng.export(s, src, [eng.TextOverlay(0, 60, 300, "Signed by Test", 14,
                                               (0.85, 0, 0))])
    s.close()
    d = pymupdf.open(src)
    txt = d[0].get_text()
    d.close()
    check("I save over source works", "Signed by Test" in txt,
          f"warn={warn} extracted={'Signed by Test' in txt}")
    check("K spaces searchable (no NBSP)", "\xa0" not in txt.split("Signed")[-1][:30],
          repr(txt[txt.find('Signed'):txt.find('Signed') + 20]))


# ------------------------------------------------ BUG J: path error handling
def bug_j():
    s = eng.Session(os.path.join(CORPUS, "normal_letter.pdf"))
    for bad, name in [(os.path.join(CORPUS), "path-is-dir"),
                      (r"Q:\nope\out.pdf", "bad-drive")]:
        try:
            eng.export(s, bad, [eng.TextOverlay(0, 50, 50, "x", 10, (0, 0, 0))])
            check(f"J {name} clean error", False, "no exception raised")
        except eng.EngineError:
            check(f"J {name} clean error", True)
        except Exception as exc:
            check(f"J {name} clean error", False, f"raw {type(exc).__name__}: {exc}")
    s.close()


# ------------------------------------------- BUG M: NUL / surrogates / controls
def bug_m():
    src = os.path.join(CORPUS, "normal_letter.pdf")
    cases = [("nul", "before\x00after"),
             ("surrogate", "ab" + chr(0xD800) + "cd"),
             ("controls", "a\x07b\x1bc")]
    for name, txt in cases:
        s = eng.Session(src)
        outp = os.path.join(OUT, f"m_{name}.pdf")
        try:
            eng.export(s, outp, [eng.TextOverlay(0, 60, 300, txt, 16, (0.85, 0, 0))])
            d = pymupdf.open(outp)
            pix = d[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
            ink = count_px(pix, lambda p: p[0] > 140 and p[1] < 90 and p[2] < 90,
                           (110, 590, 800, 680))
            d.close()
            check(f"M {name} renders", ink > 3, f"ink={ink}")
        except Exception as exc:
            check(f"M {name} renders", False, f"{type(exc).__name__}: {exc}")
        finally:
            s.close()


# ------------------------------------------------ BUG N: /Rotate 45 consistency
def bug_n():
    src = os.path.join(CORPUS, "rot_630.pdf")  # has raw /Rotate 630
    # build a /Rotate 45 file
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 60), "Rot45 doc", fontsize=14)
    p45 = os.path.join(OUT, "rot45.pdf")
    doc.save(p45)
    doc.close()
    raw = open(p45, "rb").read()
    for pat in (b"/Rotate 0", b"/MediaBox[0 0 612 792]"):
        pass
    if b"/Rotate" not in raw:
        raw = raw.replace(b"/MediaBox[0 0 612 792]",
                          b"/MediaBox[0 0 612 792]/Rotate 45", 1)
        raw = raw.replace(b"/MediaBox [0 0 612 792]",
                          b"/MediaBox [0 0 612 792] /Rotate 45", 1)
    else:
        raw = raw.replace(b"/Rotate 0", b"/Rotate 45", 1)
    open(p45, "wb").write(raw)
    s = eng.Session(p45)
    img_r, z = s.render_page(0, 1.0)
    pw, ph = s.page_size(0)
    tx, ty = pw * 0.5, ph * 0.5
    outp = os.path.join(OUT, "n_rot45_out.pdf")
    eng.export(s, outp, [eng.ImageOverlay(0, tx - 20, ty - 20, tx + 20, ty + 20,
                                          png=magenta_png())])
    s.close()
    d = pymupdf.open(outp)
    pix = d[0].get_pixmap()
    c = centroid_mag(pix)
    d.close()
    # WYSIWYG contract: stamp lands where the app's own render says it is
    ok = c is not None and abs(c[0] - tx) < 6 and abs(c[1] - ty) < 6
    check("N /Rotate 45 render/export agree", ok,
          f"page={pw:.0f}x{ph:.0f} centroid={c} want=({tx:.0f},{ty:.0f})")


# ------------------------------------------------ BUG L: htmlbox leading spaces
def bug_l():
    src = os.path.join(CORPUS, "normal_letter.pdf")
    xs = []
    for name, txt in (("noindent", "中文字"), ("indent", "        中文字")):
        s = eng.Session(src)
        outp = os.path.join(OUT, f"l_{name}.pdf")
        eng.export(s, outp, [eng.TextOverlay(0, 60, 300, txt, 16, (0, 0, 0))])
        s.close()
        d = pymupdf.open(outp)
        pix = d[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
        first_x = None
        for x in range(100, 1100, 2):
            col = sum(1 for y in range(590, 700, 2)
                      if sum(pix.pixel(x, y)[:3]) < 690)
            if col:
                first_x = x
                break
        d.close()
        xs.append(first_x)
    check("L htmlbox leading spaces preserved",
          xs[0] is not None and xs[1] is not None and xs[1] - xs[0] > 30,
          f"first_ink_x: {xs}")


# ---------------------------------------- BUG O: huge images downscale politely
def bug_o():
    img = Image.new("L", (15000, 13000), 255)  # 195MP, > old 179MP limit
    p = os.path.join(OUT, "huge.png")
    img.save(p, "PNG")
    try:
        png, w, h = eng.prepare_image(p)
        check("O 195MP image accepted+downscaled", max(w, h) <= 3000, f"{w}x{h}")
    except Exception as exc:
        check("O 195MP image accepted+downscaled", False,
              f"{type(exc).__name__}: {exc}")


# ------------------------------------- E2: source mutated after open (stream-open)
def bug_e2():
    import shutil
    src = os.path.join(OUT, "mutate.pdf")
    shutil.copy(os.path.join(CORPUS, "normal_letter.pdf"), src)
    s = eng.Session(src)
    # truncate the source on disk while the session is open
    with open(src, "r+b") as fh:
        fh.truncate(100)
    outp = os.path.join(OUT, "e2_out.pdf")
    warn = eng.export(s, outp, [eng.TextOverlay(0, 60, 300, "Still fine", 14,
                                                (0.85, 0, 0))])
    s.close()
    d = pymupdf.open(outp)
    ok = "Still fine" in d[0].get_text()
    d.close()
    check("E2 source truncated under session", ok, f"warn={warn}")
    # and deleting the source entirely
    src2 = os.path.join(OUT, "delete_me.pdf")
    shutil.copy(os.path.join(CORPUS, "normal_letter.pdf"), src2)
    s = eng.Session(src2)
    os.remove(src2)
    outp2 = os.path.join(OUT, "e2_out2.pdf")
    warn = eng.export(s, outp2, [eng.TextOverlay(0, 60, 300, "Ghost", 14,
                                                 (0.85, 0, 0))])
    s.close()
    d = pymupdf.open(outp2)
    ok = "Ghost" in d[0].get_text()
    d.close()
    check("E2 source deleted under session", ok, f"warn={warn}")


# ==================== ROUND 2 (post-regression-workflow) ====================


def _make_raw_pdf(path, mb, crop, rot):
    """Build a PDF with REAL effective MediaBox/CropBox/Rotate values.

    (An earlier version string-injected '/Rotate N' before pymupdf's own
    '/Rotate 0'; the later duplicate key won, so every 'rotated' case
    silently ran at rotation 0. xref surgery + assertion prevents that.)
    """
    doc = pymupdf.open()
    page = doc.new_page(width=mb[2] - mb[0], height=mb[3] - mb[1])
    doc.xref_set_key(page.xref, "MediaBox",
                     f"[{mb[0]:g} {mb[1]:g} {mb[2]:g} {mb[3]:g}]")
    if crop:
        doc.xref_set_key(page.xref, "CropBox",
                         f"[{crop[0]:g} {crop[1]:g} {crop[2]:g} {crop[3]:g}]")
    doc.xref_set_key(page.xref, "Rotate", str(rot))
    data = doc.tobytes()
    doc.close()
    open(path, "wb").write(data)
    chk = pymupdf.open(path)
    assert chk[0].rotation % 360 == rot % 360, \
        f"builder broken: wanted rot {rot}, got {chk[0].rotation}"
    chk.close()


def r2_geometry():
    MBS = [(0, 0, 612, 792), (20, 30, 632, 822), (-72, -72, 540, 720),
           (-100, 50, 512, 842)]
    n = 0
    bad = []
    for mb in MBS:
        crops = [None,
                 (mb[0] + 40, mb[1] + 60, mb[2] - 30, mb[3] - 50),
                 (mb[0] - 60, mb[1] - 60, mb[2] + 90, mb[3] + 110),
                 mb,                                          # CropBox == MediaBox
                 (mb[0] - 30, mb[1] + 40, mb[2] - 50, mb[3] + 80)]  # partial overhang
        for crop in crops:
            for rot in (0, 90, 180, 270):
                n += 1
                p = os.path.join(OUT, "r2g.pdf")
                _make_raw_pdf(p, mb, crop, rot)
                s = eng.Session(p)
                pw, ph = s.page_size(0)
                tx, ty = pw * 0.45, ph * 0.35
                outp = os.path.join(OUT, "r2g_out.pdf")
                eng.export(s, outp, [eng.ImageOverlay(
                    0, tx - 20, ty - 20, tx + 20, ty + 20, png=magenta_png(40, 40))])
                s.close()
                d = pymupdf.open(outp)
                c = centroid_mag(d[0].get_pixmap())
                d.close()
                if not (c and abs(c[0] - tx) < 4 and abs(c[1] - ty) < 4):
                    bad.append((mb, crop, rot, c))
    check("R2 geometry full matrix", not bad, f"{n - len(bad)}/{n} bad={bad[:3]}")


def r2_dark16():
    cases = [("black16", "I;16", 0, lambda v: v < 40),
             ("dark16", "I;16", 7710, lambda v: v < 60),
             ("light16", "I;16", 60000, lambda v: v > 200),
             ("blackF", "F", 0.0, lambda v: v < 40)]
    for name, mode, val, pred in cases:
        img = Image.new(mode, (40, 30), val)
        p = os.path.join(OUT, f"r2_{name}.tif")
        img.save(p, "TIFF")
        png, w, h = eng.prepare_image(p)
        out = Image.open(io.BytesIO(png)).convert("L")
        v = out.getpixel((20, 15))
        check(f"R2 {name} keeps brightness", pred(v), f"lum={v}")


def r2_baseline():
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    p = os.path.join(OUT, "r2_blank.pdf")
    doc.save(p)
    doc.close()
    s = eng.Session(p)
    outp = os.path.join(OUT, "r2_baseline.pdf")
    eng.export(s, outp, [eng.TextOverlay(0, 50, 100, "XXXX", 40, (0, 0, 0))],
               verify=False)
    s.close()
    d = pymupdf.open(outp)
    pix = d[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
    top = None
    for y in range(0, pix.height):
        if any(sum(pix.pixel(x, y)[:3]) < 400 for x in range(90, 250, 2)):
            top = y / 2.0
            break
    d.close()
    # glyph top = y + (ascender - capheight)*size ~= 100 + 7.5 for Arial metrics
    check("R2 helv baseline parity", top is not None and 104 <= top <= 111,
          f"glyph_top={top}")


def r2_overflow_warn():
    s = eng.Session(os.path.join(CORPUS, "normal_letter.pdf"))
    outp = os.path.join(OUT, "r2_overflow.pdf")
    warn = eng.export(s, outp, [eng.TextOverlay(0, 60, 700, "line\n" * 40, 14,
                                                (0, 0, 0))])
    s.close()
    check("R2 overflow warned", any("beyond the page" in w for w in warn),
          f"warn={warn}")


def r2_nul_path():
    s = eng.Session(os.path.join(CORPUS, "normal_letter.pdf"))
    try:
        eng.export(s, os.path.join(OUT, "o\x00ut.pdf"),
                   [eng.TextOverlay(0, 50, 50, "x", 10, (0, 0, 0))])
        check("R2 NUL out_path clean error", False, "no exception")
    except eng.EngineError:
        check("R2 NUL out_path clean error", True)
    except Exception as exc:
        check("R2 NUL out_path clean error", False, f"{type(exc).__name__}")
    finally:
        s.close()


def r2_lightgray_no_false_warn():
    img = Image.new("RGBA", (80, 40), (235, 235, 235, 255))
    b = io.BytesIO()
    img.save(b, "PNG")
    s = eng.Session(os.path.join(CORPUS, "normal_letter.pdf"))
    outp = os.path.join(OUT, "r2_lightgray.pdf")
    warn = eng.export(s, outp, [eng.ImageOverlay(0, 300, 500, 380, 540,
                                                 png=b.getvalue())])
    s.close()
    check("R2 light-gray stamp no false warning",
          not any("not appear" in w or "visible" in w for w in warn),
          f"warn={warn}")


def r2_rotate_variants():
    # inherited /Rotate 45 on the Pages node; real-numbered 45.0 on the page
    for name, mode in (("inherited45", "pages"), ("real45", "page")):
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((300, 400), "body", fontsize=10)
        if mode == "pages":
            cat = doc.pdf_catalog()
            t, v = doc.xref_get_key(cat, "Pages")
            pages_xref = int(v.split()[0])
            doc.xref_set_key(pages_xref, "Rotate", "45")
        else:
            doc.xref_set_key(page.xref, "Rotate", "45.0")
        p = os.path.join(OUT, f"r2_{name}.pdf")
        doc.save(p)
        doc.close()
        s = eng.Session(p)
        pw, ph = s.page_size(0)
        tx, ty = pw * 0.5, ph * 0.5
        outp = os.path.join(OUT, f"r2_{name}_out.pdf")
        eng.export(s, outp, [eng.ImageOverlay(0, tx - 20, ty - 20, tx + 20,
                                              ty + 20, png=magenta_png(40, 40))])
        s.close()
        d = pymupdf.open(outp)
        c = centroid_mag(d[0].get_pixmap())
        d.close()
        ok = c and abs(c[0] - tx) < 6 and abs(c[1] - ty) < 6
        check(f"R2 {name} render/export agree", ok,
              f"centroid={c} want=({tx:.0f},{ty:.0f})")


def r2_cyrillic_clean_text():
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    p = os.path.join(OUT, "r2_blank2.pdf")
    doc.save(p)
    doc.close()
    s = eng.Session(p)
    outp = os.path.join(OUT, "r2_cyr.pdf")
    eng.export(s, outp, [eng.TextOverlay(0, 40, 100, "Подпись Тест", 16,
                                         (0, 0, 0))], verify=False)
    s.close()
    d = pymupdf.open(outp)
    txt = d[0].get_text()
    pix = d[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
    ink = count_px(pix, lambda px: sum(px[:3]) < 400, (70, 190, 400, 260))
    d.close()
    check("R2 cyrillic renders", ink > 20, f"ink={ink}")
    check("R2 cyrillic clean spaces", "Подпись" in txt and "\xa0" not in txt,
          repr(txt.strip()[:40]))


def main():
    for fn in (bug_a, bug_b, bug_c, bug_d, bug_e, bug_ik, bug_j, bug_m,
               bug_n, bug_l, bug_o, bug_e2,
               r2_geometry, r2_dark16, r2_baseline, r2_overflow_warn,
               r2_nul_path, r2_lightgray_no_false_warn, r2_rotate_variants,
               r2_cyrillic_clean_text):
        try:
            fn()
        except Exception:
            fails.append(fn.__name__)
            print(f"FAIL  {fn.__name__} EXCEPTION:\n{traceback.format_exc()}")
    print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILURES: {fails}'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
