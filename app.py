"""NiccoPDF — dead-simple PDF signer / text stamper (Windows + macOS).

Open a PDF (or image), click to add text boxes, drop in a signature image
(drag to move, corner-drag to resize), then Save As PDF.

Usage:
    pythonw app.py [file.pdf]        (Windows)
    python3 app.py [file.pdf]        (macOS / Linux)
    python  app.py --selftest <in.pdf> <out.pdf>   (headless smoke test)
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import time
import traceback

APP_NAME = "NiccoPDF"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    CONF_DIR = os.path.join(os.environ.get("APPDATA", APP_DIR), APP_NAME)
elif IS_MAC:
    CONF_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
else:
    CONF_DIR = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        APP_NAME)
CONF_PATH = os.path.join(CONF_DIR, "config.json")
LOG_PATH = os.path.join(CONF_DIR, "error.log")


def _open_in_default_app(path: str) -> None:
    if hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        import subprocess
        opener = "open" if IS_MAC else "xdg-open"
        subprocess.Popen([opener, path])


def _log(msg: str) -> None:
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _fatal_box(title: str, msg: str) -> None:
    try:
        if IS_WIN:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
        elif IS_MAC:
            import json as _json
            import subprocess
            subprocess.run(
                ["osascript", "-e",
                 f"display dialog {_json.dumps(msg)} with title "
                 f"{_json.dumps(title)} buttons {{\"OK\"}} default button 1"],
                timeout=120, check=False)
        else:
            raise OSError("no native dialog")
    except Exception:
        print(f"{title}: {msg}", file=sys.stderr)


try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, simpledialog

    from PIL import Image, ImageTk

    sys.path.insert(0, APP_DIR)
    import pdf_engine as eng
except Exception:
    _log("startup import failure:\n" + traceback.format_exc())
    _fatal_box(APP_NAME, "NiccoPDF could not start (missing component).\n\n"
               + traceback.format_exc(limit=2)
               + f"\nDetails logged to:\n{LOG_PATH}")
    raise SystemExit(1)

COLORS = {"Black": "#000000", "Blue": "#1a3faa", "Red": "#c00000"}
MIN_ZOOM, MAX_ZOOM = 0.08, 6.0
HANDLE = 9  # px, selection handle square size


def hex_to_rgb01(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore


# ---------------------------------------------------------------------------
# GUI overlay model (mutable; converted to engine dataclasses on export)
# ---------------------------------------------------------------------------


class GText:
    kind = "text"

    def __init__(self, page: int, x: float, y: float, text: str,
                 size: float, color: str):
        self.page, self.x, self.y = page, x, y
        self.text, self.size, self.color = text, size, color


class GImage:
    kind = "image"

    def __init__(self, page: int, x: float, y: float, w: float, h: float,
                 png: bytes, pil: Image.Image):
        self.page, self.x, self.y, self.w, self.h = page, x, y, w, h
        self.png = png          # full-res RGBA PNG for export
        self.pil = pil          # PIL image for display
        self._disp_key = None   # (w_px, h_px) of cached PhotoImage
        self._disp_photo = None

    def photo(self, w_px: int, h_px: int):
        w_px, h_px = max(1, w_px), max(1, h_px)
        if self._disp_key != (w_px, h_px):
            self._disp_photo = ImageTk.PhotoImage(
                self.pil.resize((w_px, h_px), Image.Resampling.BILINEAR))
            self._disp_key = (w_px, h_px)
        return self._disp_photo


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1180x860")
        root.minsize(720, 480)

        self.session: eng.Session | None = None
        self.pdf_path: str | None = None
        self.page = 0
        self.zoom = 1.0
        self.overlays: list = []
        self.undo_stack: list = []
        self.dirty = False

        self.selected: int | None = None   # index into self.overlays
        self.mode = "idle"                 # idle | text | image
        self.pending_image = None          # dict(png, pil, w_px, h_px)
        self.editing = None                # dict for open inline editor
        self.drag = None
        self._page_cache: dict = {}
        self._photo = None
        self._font_cache: dict = {}
        self._last_err_ts = 0.0

        self.conf = self._load_conf()

        self._build_ui()
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._tk_error

    # ------------------------------------------------------------- config
    def _load_conf(self) -> dict:
        try:
            with open(CONF_PATH, "r", encoding="utf-8") as fh:
                c = json.load(fh)
                if isinstance(c, dict):
                    return c
        except Exception:
            pass
        return {}

    def _save_conf(self) -> None:
        try:
            os.makedirs(CONF_DIR, exist_ok=True)
            self.conf["font_size"] = self._cur_font_size()
            self.conf["color"] = self.color_var.get()
            self.conf["white_alpha"] = bool(self.white_var.get())
            with open(CONF_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.conf, fh, indent=1)
        except Exception:
            pass

    # ------------------------------------------------------------- errors
    def _tk_error(self, etype, evalue, tb) -> None:
        _log("".join(traceback.format_exception(etype, evalue, tb)))
        if time.monotonic() - self._last_err_ts > 2:
            self._last_err_ts = time.monotonic()
            messagebox.showerror(APP_NAME,
                                 f"Something went wrong:\n\n{evalue}\n\n"
                                 f"(Details logged to {LOG_PATH})",
                                 parent=self.root)

    # ------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        bar = tk.Frame(self.root, bd=0, relief="flat", padx=4, pady=4)
        bar.pack(side="top", fill="x")

        def btn(text, cmd, **kw):
            b = tk.Button(bar, text=text, command=cmd, padx=8, pady=3, **kw)
            b.pack(side="left", padx=2)
            return b

        btn("📂 Open", self.cmd_open)
        self.save_btn = btn("💾 Save As PDF", self.cmd_save)
        tk.Frame(bar, width=2, bg="#c8c8c8", height=28).pack(side="left", padx=6, fill="y")

        btn("T  Add Text", self.cmd_add_text)
        btn("🖊 Signature", self.cmd_signature)
        btn("🖼 Image…", self.cmd_choose_image)
        self.white_var = tk.IntVar(value=1 if self.conf.get("white_alpha", True) else 0)
        tk.Checkbutton(bar, text="White → transparent", variable=self.white_var
                       ).pack(side="left", padx=4)
        tk.Frame(bar, width=2, bg="#c8c8c8", height=28).pack(side="left", padx=6, fill="y")

        tk.Label(bar, text="Size").pack(side="left")
        self.size_var = tk.StringVar(value=str(self.conf.get("font_size", 14)))
        sp = tk.Spinbox(bar, from_=6, to=120, width=4, textvariable=self.size_var,
                        command=self._on_size_change)
        sp.pack(side="left", padx=2)
        sp.bind("<KeyRelease>", lambda e: self._on_size_change())
        sp.bind("<Return>", lambda e: self.canvas.focus_set())
        self.color_var = tk.StringVar(value=self.conf.get("color", "Black"))
        om = tk.OptionMenu(bar, self.color_var, *COLORS, command=lambda *_: self._on_color_change())
        om.config(padx=6)
        om.pack(side="left", padx=2)

        tk.Frame(bar, width=2, bg="#c8c8c8", height=28).pack(side="left", padx=6, fill="y")
        btn("↶ Undo", self.cmd_undo)
        btn("🗑 Delete", self.cmd_delete)

        # right side: zoom + pages
        right = tk.Frame(bar)
        right.pack(side="right")
        tk.Button(right, text="−", width=2, command=lambda: self.cmd_zoom(1 / 1.2)
                  ).pack(side="left")
        self.zoom_lbl = tk.Label(right, text="100%", width=5)
        self.zoom_lbl.pack(side="left")
        tk.Button(right, text="+", width=2, command=lambda: self.cmd_zoom(1.2)
                  ).pack(side="left")
        tk.Button(right, text="Fit", command=self.cmd_fit).pack(side="left", padx=(2, 10))
        tk.Button(right, text="◀", width=2, command=lambda: self.cmd_page(-1)
                  ).pack(side="left")
        self.page_lbl = tk.Label(right, text="– / –", width=8)
        self.page_lbl.pack(side="left")
        tk.Button(right, text="▶", width=2, command=lambda: self.cmd_page(1)
                  ).pack(side="left")

        # canvas
        wrap = tk.Frame(self.root)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg="#525659", highlightthickness=0)
        vs = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        hs = tk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Button-3>", self.on_right_click)
        if IS_MAC:
            # macOS: right button reports as Button-2 under Aqua Tk, and
            # Control-click is the conventional context-menu gesture
            self.canvas.bind("<Button-2>", self.on_right_click)
            self.canvas.bind("<Control-Button-1>", self.on_right_click)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Shift-MouseWheel>",
                         lambda e: self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Control-MouseWheel>",
                         lambda e: self.cmd_zoom(1.1 if e.delta > 0 else 1 / 1.1))
        if IS_MAC:
            self.canvas.bind("<Command-MouseWheel>",
                             lambda e: self.cmd_zoom(1.1 if e.delta > 0 else 1 / 1.1))
        self.canvas.bind("<Motion>", self.on_motion)

        self.status = tk.Label(self.root, text="Open a PDF to get started.",
                               anchor="w", bd=1, relief="sunken", padx=6)
        self.status.pack(side="bottom", fill="x")

    def _bind_keys(self) -> None:
        r = self.root
        r.bind("<Control-o>", lambda e: self.cmd_open())
        r.bind("<Control-s>", lambda e: self.cmd_save())
        r.bind("<Control-z>", lambda e: self.cmd_undo(from_key=True))
        r.bind("<Delete>", lambda e: self.cmd_delete(from_key=True))
        r.bind("<Escape>", lambda e: self.cmd_escape())
        r.bind("<Prior>", lambda e: self.cmd_page(-1))
        r.bind("<Next>", lambda e: self.cmd_page(1))
        r.bind("<Control-plus>", lambda e: self.cmd_zoom(1.2))
        r.bind("<Control-equal>", lambda e: self.cmd_zoom(1.2))
        r.bind("<Control-minus>", lambda e: self.cmd_zoom(1 / 1.2))
        if IS_MAC:
            r.bind("<Command-o>", lambda e: self.cmd_open())
            r.bind("<Command-s>", lambda e: self.cmd_save())
            r.bind("<Command-z>", lambda e: self.cmd_undo(from_key=True))
            r.bind("<Command-plus>", lambda e: self.cmd_zoom(1.2))
            r.bind("<Command-equal>", lambda e: self.cmd_zoom(1.2))
            r.bind("<Command-minus>", lambda e: self.cmd_zoom(1 / 1.2))
            # Mac keyboards: the main "delete" key sends BackSpace in Tk
            r.bind("<BackSpace>", lambda e: self.cmd_delete(from_key=True))
        for key, dx, dy in (("<Left>", -1, 0), ("<Right>", 1, 0),
                            ("<Up>", 0, -1), ("<Down>", 0, 1)):
            r.bind(key, lambda e, dx=dx, dy=dy: self.cmd_nudge(dx, dy, 1))
            r.bind(f"<Shift-{key[1:-1]}>",
                   lambda e, dx=dx, dy=dy: self.cmd_nudge(dx, dy, 10))

    def set_status(self, msg: str) -> None:
        self.status.config(text=msg)

    # ------------------------------------------------------------- helpers
    def _cur_font_size(self) -> float:
        try:
            v = float(self.size_var.get())
            return min(200.0, max(4.0, v))
        except Exception:
            return 14.0

    def _tkfont(self, size_px: int):
        size_px = max(2, size_px)
        if size_px not in self._font_cache:
            self._font_cache[size_px] = tkfont.Font(family="Arial", size=-size_px)
        return self._font_cache[size_px]

    def _set_cursor(self, name: str) -> None:
        """Set the canvas cursor, tolerating names a platform's Tk lacks."""
        if getattr(self, "_cursor_now", None) == name:
            return
        self._cursor_now = name
        try:
            self.canvas.config(cursor=name)
        except tk.TclError:
            try:
                self.canvas.config(cursor="")
            except tk.TclError:
                pass

    def _typing_elsewhere(self) -> bool:
        """True when keyboard focus is in an entry-like widget (so global
        Delete/arrow/undo shortcuts must not touch canvas items)."""
        try:
            w = self.root.focus_get()
        except Exception:
            return False
        return isinstance(w, (tk.Entry, tk.Spinbox, tk.Text, tk.Listbox))

    def _mark_dirty(self) -> None:
        self.dirty = True
        base = os.path.basename(self.pdf_path) if self.pdf_path else ""
        self.root.title(f"*{base} — {APP_NAME}" if base else APP_NAME)

    def push_undo(self) -> None:
        snap = [copy.copy(o) for o in self.overlays]
        self.undo_stack.append(snap)
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)

    # coordinate transforms -------------------------------------------------
    def pt_from_canvas(self, cx: float, cy: float) -> tuple[float, float]:
        return (cx - self.ox) / self.zoom, (cy - self.oy) / self.zoom

    def canvas_from_pt(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.zoom, self.oy + y * self.zoom

    def _evt_canvas(self, e) -> tuple[float, float]:
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    # ------------------------------------------------------------- open
    def cmd_open(self) -> None:
        self._commit_editor()
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Open PDF or image",
            initialdir=self.conf.get("last_dir", os.path.expanduser("~")),
            filetypes=[("PDF and images",
                        "*.pdf;*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.tif;*.tiff;*.webp"),
                       ("All files", "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        try:
            sess = eng.Session(path)
        except eng.EngineError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        except Exception as exc:
            _log(traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Could not open this file:\n{exc}",
                                 parent=self.root)
            return
        while sess.needs_password:
            pw = simpledialog.askstring(
                APP_NAME, "This PDF is password-protected.\nEnter the password:",
                show="•", parent=self.root)
            if pw is None:
                sess.close()
                return
            if not sess.authenticate(pw):
                messagebox.showwarning(APP_NAME, "That password didn't work — try again.",
                                       parent=self.root)
        if sess.page_count == 0:
            messagebox.showerror(APP_NAME, "This PDF contains no pages.", parent=self.root)
            sess.close()
            return
        try:
            sess.render_page(0, 0.3)
        except eng.EngineError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            sess.close()
            return

        if self.session:
            self.session.close()
        self.session = sess
        self.pdf_path = os.path.abspath(path)
        self.page = 0
        self.overlays = []
        self.undo_stack = []
        self.dirty = False
        self.selected = None
        self.mode = "idle"
        self.editing = None
        self._page_cache.clear()
        self.conf["last_dir"] = os.path.dirname(self.pdf_path)
        self._save_conf()
        self.root.title(f"{os.path.basename(path)} — {APP_NAME}")
        self.cmd_fit()
        hint = ""
        if sess.is_form:
            hint = "  (This PDF has form fields — they'll be flattened into the page on save.)"
        self.set_status(f"Loaded {sess.page_count} page(s).{hint}  "
                        "Use “Add Text” or “Signature”, then Save As PDF.")

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        return messagebox.askyesno(
            APP_NAME, "You have unsaved additions on this PDF.\nDiscard them?",
            parent=self.root)

    # ------------------------------------------------------------- save
    def cmd_save(self) -> None:
        if not self.session or not self.pdf_path:
            messagebox.showinfo(APP_NAME, "Open a PDF first.", parent=self.root)
            return
        self._commit_editor()
        stem, _ = os.path.splitext(os.path.basename(self.pdf_path))
        out = filedialog.asksaveasfilename(
            parent=self.root, title="Save signed PDF as…",
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialdir=os.path.dirname(self.pdf_path),
            initialfile=f"{stem}_signed.pdf")
        if not out:
            return
        try:
            warnings = eng.export(self.session, out, self._engine_overlays())
        except eng.EngineError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        except Exception as exc:
            _log(traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Saving failed:\n{exc}", parent=self.root)
            return
        self.dirty = False
        self.root.title(f"{os.path.basename(self.pdf_path)} — {APP_NAME}")
        self._save_conf()
        msg = f"Saved:\n{out}"
        if warnings:
            msg += "\n\nNotes:\n• " + "\n• ".join(warnings)
        if messagebox.askyesno(APP_NAME, msg + "\n\nOpen it now?", parent=self.root):
            try:
                _open_in_default_app(out)
            except Exception as exc:
                messagebox.showwarning(APP_NAME, f"Could not open the file:\n{exc}",
                                       parent=self.root)

    def _engine_overlays(self) -> list:
        out = []
        for o in self.overlays:
            if o.kind == "text":
                if o.text.strip():
                    out.append(eng.TextOverlay(page=o.page, x=o.x, y=o.y, text=o.text,
                                               size=o.size, color=hex_to_rgb01(o.color)))
            else:
                out.append(eng.ImageOverlay(page=o.page, x0=o.x, y0=o.y,
                                            x1=o.x + o.w, y1=o.y + o.h, png=o.png))
        return out

    # ------------------------------------------------------------- add text
    def cmd_add_text(self) -> None:
        if not self.session:
            messagebox.showinfo(APP_NAME, "Open a PDF first.", parent=self.root)
            return
        self._commit_editor()
        self.mode = "text"
        self._set_cursor("crosshair")
        self.set_status("Click on the page where the text should start.  (Esc cancels)")

    # ------------------------------------------------------------- images
    def cmd_signature(self) -> None:
        if not self.session:
            messagebox.showinfo(APP_NAME, "Open a PDF first.", parent=self.root)
            return
        last = self.conf.get("last_signature")
        if last and os.path.exists(last):
            self._start_image_placement(last)
        else:
            self.cmd_choose_image(remember_as_signature=True)

    def cmd_choose_image(self, remember_as_signature: bool = True) -> None:
        if not self.session:
            messagebox.showinfo(APP_NAME, "Open a PDF first.", parent=self.root)
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose signature / image",
            initialdir=self.conf.get("last_img_dir", self.conf.get("last_dir", "")),
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.tif;*.tiff;*.webp"),
                       ("All files", "*.*")])
        if not path:
            return
        self.conf["last_img_dir"] = os.path.dirname(path)
        if remember_as_signature:
            self.conf["last_signature"] = path
        self._save_conf()
        self._start_image_placement(path)

    def _start_image_placement(self, path: str) -> None:
        self._commit_editor()
        try:
            png, w_px, h_px = eng.prepare_image(path,
                                                white_to_alpha=bool(self.white_var.get()))
        except eng.EngineError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        except Exception as exc:
            _log(traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Could not load that image:\n{exc}",
                                 parent=self.root)
            return
        pil = Image.open(io.BytesIO(png)).convert("RGBA")
        self.pending_image = {"png": png, "pil": pil, "w": w_px, "h": h_px,
                              "name": os.path.basename(path)}
        self.mode = "image"
        self._set_cursor("crosshair")
        self.set_status(f"Click on the page to place “{os.path.basename(path)}”.  "
                        "(Esc cancels — drag corners afterwards to resize)")

    def _place_pending_image(self, x_pt: float, y_pt: float) -> None:
        pi = self.pending_image
        if not pi or not self.session:
            return
        pw, ph = self.session.page_size(self.page)
        w_pt = min(pw * 0.30, pi["w"] * 0.75)
        w_pt = max(w_pt, 12.0)
        h_pt = w_pt * pi["h"] / max(pi["w"], 1)
        x = x_pt - w_pt / 2
        y = y_pt - h_pt / 2
        # keep mostly on the page
        x = min(max(x, -w_pt * 0.5), pw - w_pt * 0.5)
        y = min(max(y, -h_pt * 0.5), ph - h_pt * 0.5)
        self.push_undo()
        ov = GImage(self.page, x, y, w_pt, h_pt, pi["png"], pi["pil"])
        self.overlays.append(ov)
        self.selected = len(self.overlays) - 1
        self.mode = "idle"
        self.pending_image = None
        self._set_cursor("")
        self._mark_dirty()
        self.redraw()
        self.set_status("Drag to move; drag a corner to resize; Del deletes; Ctrl+Z undoes.")

    # ------------------------------------------------------------- editing text
    def _open_editor(self, x_pt: float, y_pt: float, initial: str = "",
                     ov_index: int | None = None, size: float | None = None,
                     color: str | None = None) -> None:
        self._commit_editor()
        size = size or self._cur_font_size()
        color = color or COLORS.get(self.color_var.get(), "#000000")
        cx, cy = self.canvas_from_pt(x_pt, y_pt)
        px = max(8, int(round(size * self.zoom)))
        txt = tk.Text(self.canvas, width=28, height=3, font=self._tkfont(px),
                      fg=color, insertbackground=color, wrap="none",
                      bd=1, relief="solid", undo=True)
        txt.insert("1.0", initial)
        win = self.canvas.create_window(cx, cy, window=txt, anchor="nw")
        self.editing = {"widget": txt, "win": win, "x": x_pt, "y": y_pt,
                        "index": ov_index, "size": size, "color": color}
        txt.focus_set()
        txt.bind("<Escape>", lambda e: self._cancel_editor())
        txt.bind("<Control-Return>", lambda e: (self._commit_editor(), "break")[1])
        txt.bind("<FocusOut>", lambda e: self._commit_editor())
        self.set_status("Type your text.  Enter = new line · Ctrl+Enter or click away = done · Esc = cancel")

    def _commit_editor(self) -> None:
        ed = self.editing
        if not ed:
            return
        self.editing = None
        try:
            content = ed["widget"].get("1.0", "end-1c")
        except Exception:
            content = ""
        try:
            self.canvas.delete(ed["win"])
            ed["widget"].destroy()
        except Exception:
            pass
        content = content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        content = "".join(c for c in content
                          if c == "\n" or c == "\t" or ord(c) >= 32)
        content = content.replace("\t", "    ")
        self.push_undo()
        if ed["index"] is not None and 0 <= ed["index"] < len(self.overlays):
            if content.strip():
                o = self.overlays[ed["index"]]
                o.text = content
                self.selected = ed["index"]
            else:
                del self.overlays[ed["index"]]
                self.selected = None
        elif content.strip():
            self.overlays.append(GText(self.page, ed["x"], ed["y"], content,
                                       ed["size"], ed["color"]))
            self.selected = len(self.overlays) - 1
        else:
            self.undo_stack.pop()  # nothing changed
            self.redraw()
            return
        self._mark_dirty()
        self.redraw()

    def _cancel_editor(self) -> None:
        ed = self.editing
        if not ed:
            return
        self.editing = None
        try:
            self.canvas.delete(ed["win"])
            ed["widget"].destroy()
        except Exception:
            pass
        self.redraw()

    # ------------------------------------------------------------- selection ops
    def cmd_delete(self, from_key: bool = False) -> None:
        if self.editing or (from_key and self._typing_elsewhere()):
            return
        if self.selected is not None and 0 <= self.selected < len(self.overlays):
            self.push_undo()
            del self.overlays[self.selected]
            self.selected = None
            self._mark_dirty()
            self.redraw()

    def cmd_undo(self, from_key: bool = False) -> None:
        if self.editing or (from_key and self._typing_elsewhere()) or self.drag:
            return
        if self.undo_stack:
            self.overlays = self.undo_stack.pop()
            self.selected = None
            self._mark_dirty()
            self.redraw()

    def cmd_escape(self) -> None:
        if self.editing:
            self._cancel_editor()
            return
        if self.mode != "idle":
            self.mode = "idle"
            self.pending_image = None
            self._set_cursor("")
            self.set_status("Cancelled.")
        self.selected = None
        self.redraw()

    def cmd_nudge(self, dx: int, dy: int, step: float) -> None:
        if self.editing or self._typing_elsewhere() or self.selected is None:
            return
        if not (0 <= self.selected < len(self.overlays)):
            return
        self.push_undo()
        o = self.overlays[self.selected]
        o.x += dx * step
        o.y += dy * step
        self._mark_dirty()
        self.redraw()

    def _on_size_change(self) -> None:
        if self.selected is not None and 0 <= self.selected < len(self.overlays):
            o = self.overlays[self.selected]
            if o.kind == "text":
                o.size = self._cur_font_size()
                self._mark_dirty()
                self.redraw()

    def _on_color_change(self) -> None:
        if self.selected is not None and 0 <= self.selected < len(self.overlays):
            o = self.overlays[self.selected]
            if o.kind == "text":
                o.color = COLORS.get(self.color_var.get(), "#000000")
                self._mark_dirty()
                self.redraw()

    # ------------------------------------------------------------- paging/zoom
    def cmd_page(self, delta: int) -> None:
        if not self.session:
            return
        self._commit_editor()
        new = min(max(self.page + delta, 0), self.session.page_count - 1)
        if new != self.page:
            self.page = new
            self.selected = None
            self.redraw()

    def cmd_zoom(self, factor: float) -> None:
        if not self.session:
            return
        self._commit_editor()
        xf = self.canvas.xview()
        yf = self.canvas.yview()
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom * factor))
        self.redraw()
        try:
            self.canvas.xview_moveto(xf[0])
            self.canvas.yview_moveto(yf[0])
        except Exception:
            pass

    def cmd_fit(self) -> None:
        if not self.session:
            return
        self._commit_editor()
        self.root.update_idletasks()
        cw = max(self.canvas.winfo_width(), 200)
        pw, _ = self.session.page_size(self.page)
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, (cw - 40) / max(pw, 1)))
        self.redraw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    # ------------------------------------------------------------- drawing
    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        if not self.session:
            return
        key = (self.page, round(self.zoom, 4))
        cached = self._page_cache.get(key)
        if cached is None:
            try:
                img, z_eff = self.session.render_page(self.page, self.zoom)
            except Exception as exc:
                _log(traceback.format_exc())
                self.set_status(f"Could not render page {self.page + 1}: {exc}")
                return
            self.zoom = z_eff
            key = (self.page, round(self.zoom, 4))
            photo = ImageTk.PhotoImage(img)
            if len(self._page_cache) > 5:
                self._page_cache.clear()
            self._page_cache[key] = photo
            cached = photo
        self._photo = cached
        iw, ih = cached.width(), cached.height()
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        self.ox = max((cw - iw) // 2, 20)
        self.oy = max((ch - ih) // 2, 20)
        c.configure(scrollregion=(0, 0, self.ox + iw + 20, self.oy + ih + 20))
        c.create_rectangle(self.ox + 3, self.oy + 3, self.ox + iw + 3, self.oy + ih + 3,
                           fill="#3c3f41", outline="")
        c.create_image(self.ox, self.oy, image=cached, anchor="nw")

        for idx, o in enumerate(self.overlays):
            if o.page != self.page:
                continue
            if self.editing and self.editing.get("index") == idx:
                continue
            tag = f"ov{idx}"
            if o.kind == "text":
                cx, cy = self.canvas_from_pt(o.x, o.y)
                px = max(2, int(round(o.size * self.zoom)))
                c.create_text(cx, cy, text=o.text, anchor="nw",
                              font=self._tkfont(px), fill=o.color,
                              tags=("ov", tag))
            else:
                cx, cy = self.canvas_from_pt(o.x, o.y)
                wp = int(round(o.w * self.zoom))
                hp = int(round(o.h * self.zoom))
                c.create_image(cx, cy, image=o.photo(wp, hp), anchor="nw",
                               tags=("ov", tag))
            if idx == self.selected:
                self._draw_selection(o)

        # an open inline editor must survive redraws (delete("all") above
        # removed its canvas window item; the Text widget itself still lives)
        if self.editing:
            try:
                ecx, ecy = self.canvas_from_pt(self.editing["x"], self.editing["y"])
                self.editing["win"] = c.create_window(
                    ecx, ecy, window=self.editing["widget"], anchor="nw")
            except Exception:
                pass

        self.zoom_lbl.config(text=f"{self.zoom * 100:.0f}%")
        self.page_lbl.config(text=f"{self.page + 1} / {self.session.page_count}")

    def _sel_bbox_canvas(self, o) -> tuple[float, float, float, float]:
        if o.kind == "image":
            x0, y0 = self.canvas_from_pt(o.x, o.y)
            x1, y1 = self.canvas_from_pt(o.x + o.w, o.y + o.h)
            return x0, y0, x1, y1
        w_pt, h_pt = eng.measure_text(o.text, o.size)
        x0, y0 = self.canvas_from_pt(o.x, o.y)
        x1, y1 = self.canvas_from_pt(o.x + max(w_pt, 10), o.y + max(h_pt, o.size))
        return x0, y0, x1, y1

    def _draw_selection(self, o) -> None:
        c = self.canvas
        x0, y0, x1, y1 = self._sel_bbox_canvas(o)
        c.create_rectangle(x0 - 2, y0 - 2, x1 + 2, y1 + 2, outline="#2f7df6",
                           dash=(4, 3), width=1, tags=("sel",))
        corners = {"nw": (x0, y0), "ne": (x1, y0), "sw": (x0, y1), "se": (x1, y1)}
        if o.kind == "text":
            corners = {"se": (x1, y1)}
        for name, (hx, hy) in corners.items():
            c.create_rectangle(hx - HANDLE / 2, hy - HANDLE / 2,
                               hx + HANDLE / 2, hy + HANDLE / 2,
                               fill="#2f7df6", outline="white",
                               tags=("handle", f"handle_{name}"))

    # ------------------------------------------------------------- mouse
    def _hit_overlay(self, cx: float, cy: float) -> int | None:
        items = self.canvas.find_overlapping(cx - 1, cy - 1, cx + 1, cy + 1)
        for item in reversed(items):
            for t in self.canvas.gettags(item):
                if t.startswith("ov") and t != "ov":
                    try:
                        return int(t[2:])
                    except ValueError:
                        pass
        return None

    def _hit_handle(self, cx: float, cy: float) -> str | None:
        items = self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)
        for item in reversed(items):
            for t in self.canvas.gettags(item):
                if t.startswith("handle_"):
                    return t[len("handle_"):]
        return None

    def on_click(self, e) -> None:
        # clicking the page always reclaims keyboard focus (otherwise focus
        # can stay stuck in the Size spinbox and shortcuts go dead)
        try:
            self.canvas.focus_set()
        except Exception:
            pass
        if not self.session:
            return
        cx, cy = self._evt_canvas(e)
        if self.editing:
            self._commit_editor()
            return
        x_pt, y_pt = self.pt_from_canvas(cx, cy)

        if self.mode == "text":
            self.mode = "idle"
            self._set_cursor("")
            pw, ph = self.session.page_size(self.page)
            x_pt = min(max(x_pt, 0), pw - 5)
            y_pt = min(max(y_pt, 0), ph - 5)
            self._open_editor(x_pt, y_pt)
            return
        if self.mode == "image":
            self._place_pending_image(x_pt, y_pt)
            return

        handle = self._hit_handle(cx, cy)
        if handle and self.selected is not None and self.selected < len(self.overlays):
            o = self.overlays[self.selected]
            self.drag = {"type": "resize", "handle": handle, "ov": o,
                         "orig": (o.x, o.y,
                                  getattr(o, "w", 0), getattr(o, "h", 0),
                                  getattr(o, "size", 0)),
                         "start": (cx, cy),
                         "snap": [copy.copy(v) for v in self.overlays],
                         "pushed": False}
            return
        idx = self._hit_overlay(cx, cy)
        if idx is not None:
            self.selected = idx
            o = self.overlays[idx]
            if o.kind == "text":
                self.size_var.set(str(int(round(o.size))))
                for name, hx in COLORS.items():
                    if hx == o.color:
                        self.color_var.set(name)
            self.redraw()
            self.drag = {"type": "move", "ov": o, "orig": (o.x, o.y),
                         "start": (cx, cy),
                         "snap": [copy.copy(v) for v in self.overlays],
                         "pushed": False}
        else:
            if self.selected is not None:
                self.selected = None
                self.redraw()

    def on_drag_motion(self, e) -> None:
        if not self.drag:
            return
        cx, cy = self._evt_canvas(e)
        d = self.drag
        if not d["pushed"]:
            # first real motion: this drag is now a change — arm one undo level
            self.undo_stack.append(d["snap"])
            if len(self.undo_stack) > 100:
                self.undo_stack.pop(0)
            d["pushed"] = True
        sx, sy = d["start"]
        dx_pt = (cx - sx) / self.zoom
        dy_pt = (cy - sy) / self.zoom
        o = d["ov"]
        if d["type"] == "move":
            o.x = d["orig"][0] + dx_pt
            o.y = d["orig"][1] + dy_pt
            self.redraw()
        else:
            ox, oy, ow, oh, osz = d["orig"]
            h = d["handle"]
            if o.kind == "image":
                if h == "se":
                    new_w = ow + dx_pt
                elif h == "ne":
                    new_w = ow + dx_pt
                elif h == "sw":
                    new_w = ow - dx_pt
                else:  # nw
                    new_w = ow - dx_pt
                new_w = max(6.0, new_w)
                scale = new_w / max(ow, 1e-6)
                new_h = oh * scale
                if h == "se":
                    o.x, o.y = ox, oy
                elif h == "ne":
                    o.x, o.y = ox, oy + oh - new_h
                elif h == "sw":
                    o.x, o.y = ox + ow - new_w, oy
                else:
                    o.x, o.y = ox + ow - new_w, oy + oh - new_h
                o.w, o.h = new_w, new_h
                self.redraw()
            else:
                w_pt, h_pt = eng.measure_text(o.text, osz)
                scale = (max(w_pt, 20) + dx_pt) / max(w_pt, 20)
                o.size = min(200.0, max(4.0, osz * scale))
                self.size_var.set(str(int(round(o.size))))
                self.redraw()

    def on_release(self, e) -> None:
        d = self.drag
        self.drag = None
        if not d or not d.get("pushed"):
            # plain click (no motion): selection only, not a change
            return
        self._mark_dirty()
        self.redraw()

    def on_double(self, e) -> None:
        if not self.session or self.editing:
            return
        cx, cy = self._evt_canvas(e)
        idx = self._hit_overlay(cx, cy)
        if idx is not None and self.overlays[idx].kind == "text":
            o = self.overlays[idx]
            self.selected = idx
            self._open_editor(o.x, o.y, initial=o.text, ov_index=idx,
                              size=o.size, color=o.color)
            self.redraw()

    def on_right_click(self, e) -> None:
        if not self.session or self.editing:
            return
        cx, cy = self._evt_canvas(e)
        idx = self._hit_overlay(cx, cy)
        if idx is None:
            return
        self.selected = idx
        self.redraw()
        menu = tk.Menu(self.root, tearoff=0)
        o = self.overlays[idx]
        if o.kind == "text":
            menu.add_command(label="Edit text",
                             command=lambda: self._open_editor(
                                 o.x, o.y, initial=o.text, ov_index=idx,
                                 size=o.size, color=o.color))
        menu.add_command(label="Duplicate", command=lambda: self._duplicate(idx))
        menu.add_separator()
        menu.add_command(label="Delete", command=self.cmd_delete)
        menu.tk_popup(e.x_root, e.y_root)

    def _duplicate(self, idx: int) -> None:
        if not (0 <= idx < len(self.overlays)):
            return
        self.push_undo()
        o = copy.copy(self.overlays[idx])
        o.x += 14
        o.y += 14
        self.overlays.append(o)
        self.selected = len(self.overlays) - 1
        self._mark_dirty()
        self.redraw()

    def on_wheel(self, e) -> None:
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def on_motion(self, e) -> None:
        if not self.session or self.mode != "idle" or self.drag or self.editing:
            return
        cx, cy = self._evt_canvas(e)
        if self._hit_handle(cx, cy):
            self._set_cursor("sizing")
        elif self._hit_overlay(cx, cy) is not None:
            self._set_cursor("fleur")
        else:
            self._set_cursor("")

    # ------------------------------------------------------------- close
    def on_close(self) -> None:
        self._commit_editor()
        if not self._confirm_discard():
            return
        self._save_conf()
        if self.session:
            self.session.close()
        self.root.destroy()


# ---------------------------------------------------------------------------
# Selftest (headless-ish: window withdrawn, drives real GUI code paths)
# ---------------------------------------------------------------------------


def selftest(in_pdf: str, out_pdf: str) -> int:
    # never block on dialogs in headless mode
    for name in ("showerror", "showwarning", "showinfo"):
        setattr(messagebox, name, lambda *a, **k: print("DIALOG:", a, k))
    messagebox.askyesno = lambda *a, **k: False
    simpledialog.askstring = lambda *a, **k: None

    root = tk.Tk()
    root.withdraw()
    app = App(root)
    root.update_idletasks()
    try:
        app.open_file(in_pdf)
        if not app.session:
            print("SELFTEST FAIL: could not open", in_pdf)
            return 1
        root.update()
        pw, ph = app.session.page_size(0)
        # simulate: add a text overlay
        app.overlays.append(GText(0, pw * 0.15, ph * 0.15, "Selftest ✓ Text", 16, "#c00000"))
        # simulate: add an image overlay (magenta box)
        img = Image.new("RGBA", (120, 60), (255, 0, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        app.overlays.append(GImage(0, pw * 0.5, ph * 0.5, 90, 45, buf.getvalue(),
                                   img))
        app.redraw()
        root.update()
        warnings = eng.export(app.session, out_pdf, app._engine_overlays())
        app.session.close()
    except Exception as exc:
        print(f"SELFTEST FAIL: {exc}")
        traceback.print_exc()
        return 1
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    if not os.path.exists(out_pdf) or os.path.getsize(out_pdf) == 0:
        print("SELFTEST FAIL: no output written")
        return 1
    print("SELFTEST OK", f"warnings={warnings}" if warnings else "")
    return 0


def main() -> None:
    if IS_WIN:
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
        except Exception:
            pass

    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        raise SystemExit(selftest(args[1], args[2]))

    root = tk.Tk()
    app = App(root)
    if args and os.path.exists(args[0]):
        root.after(60, lambda: app.open_file(args[0]))
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _log("fatal:\n" + traceback.format_exc())
        _fatal_box(APP_NAME, "NiccoPDF hit a fatal error.\n\n"
                   + traceback.format_exc(limit=3))
        raise
