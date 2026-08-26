"""Build the macOS app bundle and both downloadable release zips.

Runnable from any OS (pure stdlib). Produces:
    dist/NiccoPDF.app/            - the double-clickable macOS bundle
    dist/NiccoPDF-macOS.zip       - the bundle + install notes, with unix
                                    executable bits set inside the zip
    dist/NiccoPDF-Windows.zip     - app + NiccoPDF.bat + README

Run:  python macos/build_app.py
"""
import io
import os
import shutil
import stat
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")

MAC_NOTES = """\
NiccoPDF for macOS — read me first
==================================

1.  Drag NiccoPDF.app anywhere you like (e.g. Applications).

2.  First open: RIGHT-CLICK the app -> Open -> Open.
    If macOS still blocks it ("Apple could not verify..."), go to
    System Settings -> Privacy & Security, scroll down to the NiccoPDF
    message, and click "Open Anyway". (The app is unsigned, not unsafe;
    you can also clear the flag in Terminal:  xattr -cr NiccoPDF.app )

3.  NiccoPDF needs Python 3 with Tk. If it isn't installed, the app
    opens python.org for you — install Python (one click), then open
    NiccoPDF again. The very first launch installs its PDF components
    (about a minute); after that it starts instantly.

Problems? See the log at
~/Library/Application Support/NiccoPDF/launcher.log
"""


def _read(relpath):
    with open(os.path.join(ROOT, relpath), "rb") as fh:
        return fh.read()


def _lf(data: bytes) -> bytes:
    """Force LF endings (shell scripts must not carry CRLF)."""
    return data.replace(b"\r\n", b"\n")


def build_bundle() -> str:
    app = os.path.join(DIST, "NiccoPDF.app")
    if os.path.exists(app):
        shutil.rmtree(app)
    macos_dir = os.path.join(app, "Contents", "MacOS")
    res_dir = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos_dir)
    os.makedirs(res_dir)

    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
        fh.write(_lf(_read("macos/Info.plist")))
    launcher = os.path.join(macos_dir, "NiccoPDF")
    with open(launcher, "wb") as fh:
        fh.write(_lf(_read("macos/bundle_launcher.sh")))
    core = os.path.join(res_dir, "nicco_launch.sh")
    with open(core, "wb") as fh:
        fh.write(_lf(_read("macos/nicco_launch.sh")))
    for name in ("app.py", "pdf_engine.py"):
        with open(os.path.join(res_dir, name), "wb") as fh:
            fh.write(_lf(_read(name)))

    if sys.platform != "win32":
        for p in (launcher, core):
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return app


def _zi(name: str, executable: bool = False) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name)
    zi.create_system = 3  # unix, so extractors honor the mode bits
    mode = 0o755 if executable else 0o644
    zi.external_attr = (stat.S_IFREG | mode) << 16
    return zi


def build_mac_zip(app: str) -> str:
    out = os.path.join(DIST, "NiccoPDF-macOS.zip")
    exec_suffixes = ("Contents/MacOS/NiccoPDF", "nicco_launch.sh")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_zi("READ ME FIRST.txt"), MAC_NOTES)
        for base, dirs, files in os.walk(app):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(base, f)
                rel = os.path.relpath(full, DIST).replace(os.sep, "/")
                is_exec = any(rel.endswith(sfx) for sfx in exec_suffixes)
                with open(full, "rb") as fh:
                    zf.writestr(_zi(rel, executable=is_exec), fh.read())
    return out


def build_windows_zip() -> str:
    out = os.path.join(DIST, "NiccoPDF-Windows.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("app.py", "pdf_engine.py", "README.md"):
            zf.writestr(_zi(f"NiccoPDF/{name}"), _read(name))
        # batch file must keep CRLF
        bat = _read("NiccoPDF.bat")
        if b"\r\n" not in bat:
            bat = bat.replace(b"\n", b"\r\n")
        zf.writestr(_zi("NiccoPDF/NiccoPDF.bat"), bat)
    return out


def main() -> None:
    os.makedirs(DIST, exist_ok=True)
    app = build_bundle()
    mz = build_mac_zip(app)
    wz = build_windows_zip()
    for p in (app, mz, wz):
        size = (sum(os.path.getsize(os.path.join(b, f))
                    for b, _d, fs in os.walk(p) for f in fs)
                if os.path.isdir(p) else os.path.getsize(p))
        print(f"built {p}  ({size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
