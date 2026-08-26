"""Headless engine test: stamp text + image on every corpus PDF, save, verify.

Run:  python tests/test_engine.py
Exit code 0 = all pass.
"""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import io

import pymupdf
from PIL import Image

import pdf_engine as eng

CORPUS = os.path.join(HERE, "corpus")
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

PASSWORDS = {"encrypted_aes256.pdf": "test123", "encrypted_rc4.pdf": "test123"}
EXPECT_OPEN_FAIL = {"empty_0byte.pdf"}
MAY_FAIL_OPEN = {"corrupt_truncated.pdf", "corrupt_junk_header.pdf"}

# magenta test stamp
_img = Image.new("RGBA", (120, 60), (255, 0, 255, 255))
_buf = io.BytesIO()
_img.save(_buf, "PNG")
MAGENTA = _buf.getvalue()

RED = (0.85, 0.0, 0.0)

results = []


def check_region(pix, x0, y0, x1, y1, pred):
    n = 0
    for x in range(max(0, int(x0)), min(pix.width, int(x1)), 2):
        for y in range(max(0, int(y0)), min(pix.height, int(y1)), 2):
            if pred(pix.pixel(x, y)):
                n += 1
    return n


def is_magenta(px):
    r, g, b = px[:3]
    return r > 170 and b > 170 and g < 110


def is_red(px):
    r, g, b = px[:3]
    return r > 140 and g < 90 and b < 90


def run_one(fname):
    path = os.path.join(CORPUS, fname)
    t0 = time.time()
    try:
        sess = eng.Session(path)
    except eng.EngineError as exc:
        if fname in EXPECT_OPEN_FAIL or fname in MAY_FAIL_OPEN:
            return ("PASS", f"clean refusal: {str(exc).splitlines()[0]}")
        return ("FAIL", f"unexpected open failure: {exc}")
    if fname in EXPECT_OPEN_FAIL:
        sess.close()
        return ("FAIL", "expected a clean refusal but it opened")
    if sess.needs_password:
        pw = PASSWORDS.get(fname)
        if pw is None:
            sess.close()
            return ("FAIL", "asked for password unexpectedly")
        if not sess.authenticate(pw):
            sess.close()
            return ("FAIL", "correct password rejected")

    n_pages = sess.page_count
    if n_pages == 0:
        sess.close()
        return ("FAIL", "0 pages after open")

    # render first page (sanity)
    img, z = sess.render_page(0, 1.5)

    overlays = []
    stamp_pages = [0] + ([n_pages - 1] if n_pages > 1 else [])
    coords = {}
    for pno in stamp_pages:
        pw_, ph_ = sess.page_size(pno)
        tx, ty = pw_ * 0.12, ph_ * 0.30
        ts = max(8.0, min(20.0, pw_ / 30))
        ix0, iy0 = pw_ * 0.45, ph_ * 0.55
        iw = max(20.0, pw_ * 0.22)
        ih = iw / 2
        overlays.append(eng.TextOverlay(page=pno, x=tx, y=ty,
                                        text="Signed by Test\nLine two",
                                        size=ts, color=RED))
        overlays.append(eng.ImageOverlay(page=pno, x0=ix0, y0=iy0,
                                         x1=ix0 + iw, y1=iy0 + ih, png=MAGENTA))
        coords[pno] = (tx, ty, ts, ix0, iy0, iw, ih)

    out_path = os.path.join(OUT, fname.replace(".pdf", "_signed.pdf"))
    warnings = eng.export(sess, out_path, overlays)
    sess.close()

    # verify output
    doc = pymupdf.open(out_path)
    problems = []
    if doc.needs_pass:
        problems.append("output still password-protected")
    if doc.page_count != n_pages:
        problems.append(f"page count changed {n_pages} -> {doc.page_count}")
    try:
        if doc.is_form_pdf:
            problems.append("output still has interactive form fields")
    except Exception:
        pass
    for pno in stamp_pages:
        tx, ty, ts, ix0, iy0, iw, ih = coords[pno]
        page = doc[pno]
        Z = 2.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(Z, Z), alpha=False)
        mag = check_region(pix, (ix0 + 2) * Z, (iy0 + 2) * Z,
                           (ix0 + iw - 2) * Z, (iy0 + ih - 2) * Z, is_magenta)
        if mag < 10:
            problems.append(f"p{pno + 1}: image stamp not visible (magenta={mag})")
        red = check_region(pix, (tx - 2) * Z, (ty - 2) * Z,
                           (tx + ts * 12) * Z, (ty + ts * 3.2) * Z, is_red)
        if red < 4:
            problems.append(f"p{pno + 1}: text stamp not visible (red={red})")
    doc.close()

    dt = time.time() - t0
    note = f"{dt:.1f}s"
    if warnings:
        note += f" warn={warnings}"
    if problems:
        return ("FAIL", "; ".join(problems) + f" [{note}]")
    return ("PASS", note)


def main():
    files = sorted(f for f in os.listdir(CORPUS) if f.lower().endswith(".pdf"))
    if not files:
        print("no corpus — run make_corpus.py first")
        return 2
    fails = 0
    for f in files:
        try:
            status, note = run_one(f)
        except Exception:
            status, note = "FAIL", "EXCEPTION:\n" + traceback.format_exc()
        if status != "PASS":
            fails += 1
        print(f"{status:4s}  {f:28s} {note}")

    # extra: unicode / emoji text paths on a plain page
    extra_texts = [
        ("ascii", "Hello World 123"),
        ("latin", "Café naïve façade Zürich"),
        ("cyrillic", "Привет мир"),
        ("cjk", "日本語テスト 中文测试 한국어"),
        ("rtl", "مرحبا بالعالم שלום"),
        ("emoji", "Signed 😀👍 done"),
        ("mixed", "OK ✓ 中文 😀 café"),
        ("long", "word " * 200),
        ("newlines", "a\n\n\nb\nc"),
    ]
    for name, txt in extra_texts:
        try:
            src = os.path.join(CORPUS, "normal_letter.pdf")
            sess = eng.Session(src)
            ov = [eng.TextOverlay(page=0, x=60, y=300, text=txt, size=14, color=RED)]
            outp = os.path.join(OUT, f"text_{name}.pdf")
            eng.export(sess, outp, ov)
            sess.close()
            doc = pymupdf.open(outp)
            pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
            # any non-white ink in the region where text was placed?
            n = check_region(pix, 118, 596, 1100, 700,
                             lambda px: sum(px[:3]) < 720)
            doc.close()
            if n < 4:
                print(f"FAIL  text:{name:10s} nothing rendered (n={n})")
                fails += 1
            else:
                print(f"PASS  text:{name:10s} ink={n}")
        except Exception:
            print(f"FAIL  text:{name:10s} EXCEPTION:\n{traceback.format_exc()}")
            fails += 1

    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURES'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
