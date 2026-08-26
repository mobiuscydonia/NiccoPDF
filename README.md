# NiccoPDF

A dead-simple app for the one thing you actually need: **someone sends you a
PDF, you put text on it, you sign it, you save it.** Works on Windows and
macOS.

## Download

| | |
|---|---|
| 🪟 **Windows** | [**NiccoPDF-Windows.zip**](https://github.com/mobiuscydonia/NiccoPDF/releases/latest/download/NiccoPDF-Windows.zip) |
| 🍎 **macOS** | [**NiccoPDF-macOS.zip**](https://github.com/mobiuscydonia/NiccoPDF/releases/latest/download/NiccoPDF-macOS.zip) |

Both need Python 3 from [python.org](https://www.python.org/downloads/)
(one-click install; on Windows tick *"Add python.exe to PATH"*). The app
installs its own PDF components automatically the first time it runs.

**Windows:** unzip, then double-click **`NiccoPDF.bat`** (you can also drag a
PDF onto it).

**macOS:** unzip, drag **`NiccoPDF.app`** wherever you like, then
**right-click → Open** the first time. If macOS says it can't verify the app,
go to *System Settings → Privacy & Security* and click **Open Anyway** — it's
unsigned, not unsafe (or clear the flag in Terminal: `xattr -cr NiccoPDF.app`).

## Use

1. **📂 Open** — pick the PDF (password-protected ones will ask for the
   password; plain images like PNG/JPG scans also work).
2. **T Add Text** — click where the text goes, type, click away when done.
   Drag to move, drag the corner handle to resize, double-click to re-edit.
3. **🖊 Signature** — first time it asks for your signature image (PNG/JPG),
   then remembers it forever; click to place, drag corners to resize.
   *White → transparent* removes the white paper background from scans.
4. **💾 Save As PDF** — writes a new `…_signed.pdf`. Form fields are flattened
   so it looks the same in every PDF viewer, and the app re-checks the saved
   file to make sure your stamps really made it in.

Shortcuts (`Ctrl` on Windows, `Cmd` on Mac): `O` open · `S` save · `Z` undo ·
`Delete`/`Backspace` delete · arrows nudge · `PgUp/PgDn` pages ·
`Ctrl`+wheel zoom · `Esc` cancel.

## Running from source

```
# Windows                                # macOS / Linux
pip install pymupdf pymupdf-fonts pillow  pip3 install pymupdf pymupdf-fonts pillow
python app.py                             python3 app.py
```

Windows keeps a `NiccoPDF.bat` launcher on the `main` branch; the `macos`
branch adds the Mac launcher (`NiccoPDF.command`, plus `macos/build_app.py`
which assembles the double-clickable `NiccoPDF.app` bundle and both release
zips).

Errors, if any, are logged to `%APPDATA%\NiccoPDF\error.log` on Windows and
`~/Library/Application Support/NiccoPDF/` on macOS.

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
