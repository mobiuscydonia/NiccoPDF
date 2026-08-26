# NiccoPDF

A dead-simple Windows app for the one thing you actually need: **someone sends
you a PDF, you put text on it, you sign it, you save it.**

## Launch

Double-click **`NiccoPDF.bat`** (or the **NiccoPDF** shortcut on your Desktop).
You can also drag a PDF onto `NiccoPDF.bat` to open it directly.

## Use

1. **📂 Open** — pick the PDF (password-protected ones will ask for the password;
   plain images like PNG/JPG scans also work).
2. **T Add Text** — click where the text goes, type, click away when done.
   Drag to move, drag the corner handle to resize, double-click to re-edit.
3. **🖊 Signature** — first time it asks for your signature image (PNG/JPG),
   then remembers it forever; click to place, drag corners to resize.
   *White → transparent* removes the white paper background from scans.
4. **💾 Save As PDF** — writes a new `…_signed.pdf`. Form fields are flattened
   so it looks the same in every PDF viewer.

Shortcuts: `Ctrl+O` open · `Ctrl+S` save · `Ctrl+Z` undo · `Del` delete ·
arrows nudge · `PgUp/PgDn` pages · `Ctrl+wheel` zoom · `Esc` cancel.

## Requirements

Python 3 with `pymupdf`, `pymupdf-fonts`, and `Pillow`
(already installed on this machine):

    pip install pymupdf pymupdf-fonts pillow

Errors, if any, are logged to `%APPDATA%\NiccoPDF\error.log`.

## Development

The app is two files: `app.py` (tkinter GUI) and `pdf_engine.py` (headless
engine that does all the PDF work — open/decrypt, render, stamp, flatten,
save, and re-render-verify the output). To run the test suites, first
generate the hostile-PDF corpus, then run both suites (each should end with
`ALL PASS`):

    python tests/make_corpus.py
    python tests/test_engine.py
    python tests/test_fixes.py

The corpus covers rotated/cropped/negative-origin pages, AES-256/RC4
encryption, owner-locked permissions, AcroForms, scanned pages, corrupt and
truncated files, and more. `tests/test_fixes.py` is the regression suite from
three rounds of adversarial testing (misplaced stamps on rotated+cropped
pages, 16-bit scans, EXIF orientation, RTL/emoji text, silent stamp loss,
save-over-source, and friends).
