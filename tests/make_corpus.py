"""Generate a corpus of hostile / diverse PDFs into tests/corpus/."""
import io
import os
import sys

import pymupdf
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
os.makedirs(CORPUS, exist_ok=True)


def p(name):
    return os.path.join(CORPUS, name)


def basic_doc(w=612, h=792, pages=1, label="Sample document"):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=w, height=h)
        page.insert_text((50, 60), f"{label} — page {i + 1}", fontsize=14)
        page.draw_line((50, 70), (min(w - 50, 550), 70))
        for j in range(5):
            page.insert_text((50, 110 + j * 24),
                             f"Line {j + 1}: lorem ipsum dolor sit amet.", fontsize=10)
    return doc


made = []


def save(doc, name, **kw):
    doc.save(p(name), **kw)
    doc.close()
    made.append(name)


# 1. plain letter
save(basic_doc(), "normal_letter.pdf")

# 2-4. rotated pages
for rot in (90, 180, 270):
    doc = basic_doc(label=f"Rotated {rot}")
    doc[0].set_rotation(rot)
    save(doc, f"rot{rot}.pdf")

# 5. multi-rotation multi-page
doc = basic_doc(pages=4, label="Mixed rotations")
for i, rot in enumerate((0, 90, 180, 270)):
    doc[i].set_rotation(rot)
save(doc, "mixed_rotations.pdf")

# 6. CropBox offset (visible area smaller than MediaBox, non-zero origin)
doc = basic_doc(label="CropBox offset")
page = doc[0]
page.set_cropbox(pymupdf.Rect(72, 72, 540, 720))
save(doc, "cropbox_offset.pdf")

# 7. A0 huge page
save(basic_doc(w=2384, h=3370, label="A0 poster"), "a0_huge.pdf")

# 8. tiny page
save(basic_doc(w=90, h=120, label="tiny"), "tiny_page.pdf")

# 9. landscape
save(basic_doc(w=792, h=612, label="Landscape"), "landscape.pdf")

# 10. AES-256 user+owner password ("test123")
doc = basic_doc(label="AES-256 encrypted")
doc.save(p("encrypted_aes256.pdf"), owner_pw="own456", user_pw="test123",
         encryption=pymupdf.PDF_ENCRYPT_AES_256)
doc.close()
made.append("encrypted_aes256.pdf")

# 11. owner-locked only (opens without password, permissions restricted)
doc = basic_doc(label="Owner locked, printing/changes disabled")
doc.save(p("owner_locked.pdf"), owner_pw="secretowner",
         encryption=pymupdf.PDF_ENCRYPT_AES_128,
         permissions=int(pymupdf.PDF_PERM_ACCESSIBILITY))
doc.close()
made.append("owner_locked.pdf")

# 12. old RC4 40-bit encryption, user pw "test123"
doc = basic_doc(label="RC4-40 encrypted")
doc.save(p("encrypted_rc4.pdf"), owner_pw="own", user_pw="test123",
         encryption=pymupdf.PDF_ENCRYPT_RC4_40)
doc.close()
made.append("encrypted_rc4.pdf")

# 13. AcroForm with fields (one prefilled)
doc = basic_doc(label="Form document")
page = doc[0]
w = pymupdf.Widget()
w.field_name = "name"
w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
w.rect = pymupdf.Rect(150, 200, 400, 225)
w.field_value = "Prefilled Name"
w.border_color = (0, 0, 0)
w.fill_color = (0.92, 0.92, 1)
page.add_widget(w)
w2 = pymupdf.Widget()
w2.field_name = "agree"
w2.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
w2.rect = pymupdf.Rect(150, 240, 165, 255)
w2.field_value = True
page.add_widget(w2)
w3 = pymupdf.Widget()
w3.field_name = "empty_field"
w3.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
w3.rect = pymupdf.Rect(150, 280, 400, 305)
w3.border_color = (0, 0, 0)
page.add_widget(w3)
save(doc, "acroform.pdf")

# 14. existing annotations + link
doc = basic_doc(label="Annotated")
page = doc[0]
page.add_highlight_annot(pymupdf.Rect(50, 105, 300, 122))
page.add_ink_annot([[(100, 400), (150, 380), (200, 420), (260, 390)]])
page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 60, 200, 75),
                  "uri": "https://example.com"})
save(doc, "annotated.pdf")

# 15. "scanned" doc: page is one big JPEG
img = Image.new("RGB", (1700, 2200), (245, 242, 235))
for yy in range(300, 1900, 120):
    for xx in range(150, 1500, 4):
        img.putpixel((xx, yy), (60, 60, 70))
buf = io.BytesIO()
img.save(buf, "JPEG", quality=70)
doc = pymupdf.open()
page = doc.new_page(width=612, height=792)
page.insert_image(page.rect, stream=buf.getvalue())
save(doc, "scanned.pdf")

# 16. corrupt: truncated at 60%
data = open(p("normal_letter.pdf"), "rb").read()
open(p("corrupt_truncated.pdf"), "wb").write(data[: int(len(data) * 0.6)])
made.append("corrupt_truncated.pdf")

# 17. corrupt: junk before header
open(p("corrupt_junk_header.pdf"), "wb").write(b"\x00\x01JUNKJUNK" * 40 + data)
made.append("corrupt_junk_header.pdf")

# 18. not a PDF: PNG bytes named .pdf
img = Image.new("RGB", (60, 60), (200, 30, 30))
img.save(p("actually_a_png.pdf"), "PNG")
made.append("actually_a_png.pdf")

# 19. zero bytes
open(p("empty_0byte.pdf"), "wb").close()
made.append("empty_0byte.pdf")

# 20. 200 pages
save(basic_doc(pages=200, label="Many pages"), "many_pages.pdf")

# 21. weird mediabox origin (negative offsets)
doc = pymupdf.open()
page = doc.new_page(width=612, height=792)
page.insert_text((50, 60), "Weird mediabox", fontsize=14)
doc.save(p("weird_mediabox.pdf"))
doc.close()
raw = open(p("weird_mediabox.pdf"), "rb").read()
raw = raw.replace(b"/MediaBox[0 0 612 792]", b"/MediaBox[-72 -72 540 720]")
raw = raw.replace(b"/MediaBox [0 0 612 792]", b"/MediaBox [-72 -72 540 720]")
open(p("weird_mediabox.pdf"), "wb").write(raw)
made.append("weird_mediabox.pdf")

# 22. PDF claiming version 2.0
data = open(p("normal_letter.pdf"), "rb").read()
open(p("pdf20_header.pdf"), "wb").write(data.replace(b"%PDF-1.7", b"%PDF-2.0", 1))
made.append("pdf20_header.pdf")

# 23. empty signature field (digital-signature placeholder)
doc = basic_doc(label="Has signature field")
page = doc[0]
try:
    ws = pymupdf.Widget()
    ws.field_name = "sig1"
    ws.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    ws.rect = pymupdf.Rect(350, 600, 550, 660)
    page.add_widget(ws)
    save(doc, "sig_field.pdf")
except Exception as e:
    print("sig_field skipped:", e)
    doc.close()

# 24. page with /Rotate 630 (non-normalized)
doc = basic_doc(label="Rotate 630")
doc[0].set_rotation(630 % 360)  # pymupdf normalizes; also patch raw below
save(doc, "rot_630.pdf")
raw = open(p("rot_630.pdf"), "rb").read()
raw = raw.replace(b"/Rotate 270", b"/Rotate 630", 1)
open(p("rot_630.pdf"), "wb").write(raw)

print(f"corpus: {len(made)} files in {CORPUS}")
for m in made:
    print(" -", m)
