#!/usr/bin/env python3
"""
LyricRenderer — Gerador de vídeos de letras de música
Renderiza estrofes com fade in/out transparente sobre fundo preto/personalizado.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont
import json
import os
import sys
import subprocess
import threading
import re
import struct
import zlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional
import colorsys

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ─────────────────────────────────────────────
#  Data Models
# ─────────────────────────────────────────────

@dataclass
class Strophe:
    id: int
    start_time: float   # seconds
    end_time: float     # seconds
    text: str

    def duration(self):
        return self.end_time - self.start_time

    def to_mm_ss(self, seconds):
        m = int(seconds) // 60
        s = int(seconds) % 60
        cs = int((seconds % 1) * 100)
        return f"{m:02d}:{s:02d}.{cs:02d}"

    def start_str(self):
        return self.to_mm_ss(self.start_time)

    def end_str(self):
        return self.to_mm_ss(self.end_time)


# Default asset paths (relative to the script's parent folder, matching legacy Node layout)
def _default_assets_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

def _default_font_path():
    p = os.path.join(_default_assets_dir(), "fonts", "mvboli.ttf")
    return p if os.path.isfile(p) else "mvboli"

def _default_bg_image():
    p = os.path.join(_default_assets_dir(), "materials", "background-fixo.png")
    return p if os.path.isfile(p) else ""

def _default_output_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(d, exist_ok=True)
    return d


@dataclass
class Project:
    title: str = "Título da Música"
    title_font: str = "mvboli"        # matches assets/fonts/mvboli.ttf
    title_size: int = 92              # same as Node config
    lyric_font: str = "mvboli"
    lyric_size: int = 67              # same as Node config
    line_spacing: int = 10            # same as Node config
    fade_duration: float = 0.8
    text_color: str = "#FFFFFF"
    title_color: str = "#FFFFFF"
    transparent_bg: bool = True       # True = no bg (alpha channel), False = solid color
    bg_color: str = "#000000"
    bg_image: str = ""                # path to background image (overrides color)
    video_width: int = 1920
    video_height: int = 1080
    fps: int = 25
    strophes: List[Strophe] = field(default_factory=list)
    audio_file: str = ""
    output_file: str = ""

    def to_dict(self):
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d):
        strophes_data = d.pop("strophes", [])
        # Remove keys not in dataclass (forward compat)
        valid = {f.name for f in Project.__dataclass_fields__.values()}
        d = {k: v for k, v in d.items() if k in valid}
        proj = cls(**d)
        proj.strophes = [Strophe(**s) for s in strophes_data]
        return proj


def parse_time(s: str) -> float:
    """Parse MM:SS or MM:SS.cc or HH:MM:SS into seconds."""
    s = s.strip()
    parts = s.replace(",", ".").split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            return float(parts[0])
    except Exception:
        raise ValueError(f"Tempo inválido: '{s}' — use MM:SS ou MM:SS.cc")


# ─────────────────────────────────────────────
#  Video Renderer (PIL + ffmpeg pipe)
# ─────────────────────────────────────────────

def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def find_font(font_name: str, size: int):
    """Try to find a system font by name, fallback to default."""
    if not HAS_PIL:
        return None

    search_dirs = [
        "C:/Windows/Fonts",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
        "/System/Library/Fonts",
        "/Library/Fonts",
    ]

    name_lower = font_name.lower().replace(" ", "")

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if not f.lower().endswith((".ttf", ".otf")):
                    continue
                fname_lower = os.path.splitext(f)[0].lower().replace(" ", "").replace("-", "").replace("_", "")
                if name_lower in fname_lower or fname_lower in name_lower:
                    try:
                        return ImageFont.truetype(os.path.join(root, f), size)
                    except Exception:
                        pass

    # Try direct path/name (works if user typed a full path)
    try:
        return ImageFont.truetype(font_name, size)
    except Exception:
        pass

    # Absolute fallback — PIL default (small but works)
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _draw_text_centered(draw, text: str, font, color, canvas_w: int, y: int):
    """Draw text horizontally centered at y."""
    try:
        bbox = font.getbbox(text)
        tw = bbox[2] - bbox[0]
        tx = (canvas_w - tw) // 2 - bbox[0]
    except Exception:
        tw = len(text) * 10
        tx = (canvas_w - tw) // 2
    draw.text((tx, y), text, font=font, fill=color)


class FrameRenderer:
    def __init__(self, project: Project):
        self.proj = project
        self.w = project.video_width
        self.h = project.video_height
        self.bg_rgb = hex_to_rgb(project.bg_color)
        self.text_rgb = hex_to_rgb(project.text_color)
        self.title_rgb = hex_to_rgb(project.title_color)
        self.fade = project.fade_duration
        self.fps = project.fps
        self.line_height = project.lyric_size + project.line_spacing + 6
        # Sort strophes by start time once
        self.strophes = sorted(project.strophes, key=lambda s: s.start_time)

        # Font — try asset path first, then system search
        font_path = os.path.join(_default_assets_dir(), "fonts", "mvboli.ttf")
        tf_src = font_path if os.path.isfile(font_path) else project.title_font
        lf_src = font_path if os.path.isfile(font_path) else project.lyric_font
        # If project explicitly set a different font name/path, use that
        if project.title_font not in ("mvboli", "MV Boli", ""):
            tf_src = project.title_font
        if project.lyric_font not in ("mvboli", "MV Boli", ""):
            lf_src = project.lyric_font
        self.title_font = find_font(tf_src, project.title_size)
        self.lyric_font = find_font(lf_src, project.lyric_size)

        # Background image (preloaded and resized once)
        self._bg_img = None
        if project.bg_image and os.path.isfile(project.bg_image):
            try:
                bg = Image.open(project.bg_image).convert("RGB")
                bg = bg.resize((self.w, self.h), Image.LANCZOS)
                self._bg_img = bg
            except Exception:
                pass

        # Transparent mode: we work in RGBA and output rgba32 to ffmpeg
        self._transparent = project.transparent_bg and not self._bg_img

    # ── Alpha curve: smooth ease-in / ease-out ────────────

    def _alpha(self, t: float, start: float, end: float) -> float:
        """Returns 0.0–1.0 with smooth fade-in and fade-out."""
        if t <= start or t >= end:
            return 0.0
        duration = end - start
        # Fade window is min(fade_setting, 30% of segment duration)
        fade = min(self.fade, duration * 0.30)
        if fade < 0.01:
            return 1.0
        if t < start + fade:
            # ease-in: smoothstep
            x = (t - start) / fade
            return x * x * (3 - 2 * x)
        if t > end - fade:
            # ease-out: smoothstep
            x = (end - t) / fade
            return x * x * (3 - 2 * x)
        return 1.0

    # ── Frame rendering ───────────────────────────────────

    def _make_base_frame(self) -> Image.Image:
        """Create the base canvas for a frame."""
        if self._transparent:
            # Fully transparent RGBA canvas
            return Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        elif self._bg_img:
            return self._bg_img.copy().convert("RGBA")
        else:
            return Image.new("RGBA", (self.w, self.h), (*self.bg_rgb, 255))

    def render_frame(self, t: float) -> Image.Image:
        """Render one frame, return RGBA PIL Image."""
        img = self._make_base_frame()
        # Draw on a separate RGBA overlay so alpha compositing is correct
        overlay = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        self._draw_title(draw, t)
        self._draw_lyric(draw, t)

        # Composite overlay onto base
        img = Image.alpha_composite(img, overlay)
        return img

    def render_frame_bytes(self, t: float) -> bytes:
        """Return raw bytes ready for the ffmpeg pipe."""
        img = self.render_frame(t)
        if self._transparent:
            return img.tobytes()          # rgba32 — 4 bytes/pixel
        else:
            return img.convert("RGB").tobytes()  # rgb24 — 3 bytes/pixel

    # ── Draw helpers ──────────────────────────────────────

    def _draw_title(self, draw, t: float):
        if not self.strophes:
            return
        t0 = self.strophes[0].start_time
        tN = self.strophes[-1].end_time
        a = self._alpha(t, t0, tN)
        if a <= 0:
            return
        color = (*self.title_rgb, int(a * 255))
        title_y = int(self.h * 0.08)
        _draw_text_centered(draw, self.proj.title, self.title_font, color, self.w, title_y)

    def _draw_lyric(self, draw, t: float):
        active = None
        for s in self.strophes:
            if s.start_time <= t <= s.end_time:
                active = s
                break
        if active is None:
            return

        a = self._alpha(t, active.start_time, active.end_time)
        if a <= 0:
            return

        color = (*self.text_rgb, int(a * 255))
        lines = active.text.strip().split("\n")

        total_h = self.line_height * len(lines)
        start_y = (self.h - total_h) // 2

        for i, line in enumerate(lines):
            if line.strip():
                _draw_text_centered(draw, line, self.lyric_font, color, self.w,
                                    start_y + i * self.line_height)

    # ── Main render ───────────────────────────────────────

    def render_video(self, output_path: str, audio_path: str = "",
                     progress_callback=None, cancel_flag=None):
        """
        Render all frames to video via ffmpeg pipe.
        - Transparent bg  → WebM (VP9 + alpha)
        - Background img or solid color → MP4 (H.264)
        stderr drained in background thread to avoid Windows pipe deadlock.
        """
        if not self.strophes:
            return False, "Nenhuma estrofe adicionada."

        duration = self.strophes[-1].end_time + 1.5
        total_frames = int(duration * self.fps)

        use_transparent = self._transparent
        pix_fmt_in  = "rgba"   if use_transparent else "rgb24"
        ext = Path(output_path).suffix.lower()

        # Force .webm for transparent output
        if use_transparent and ext != ".webm":
            output_path = Path(output_path).with_suffix(".webm").as_posix()

        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", pix_fmt_in,
            "-s", f"{self.w}x{self.h}",
            "-r", str(self.fps),
            "-i", "pipe:0",
        ]

        has_audio = bool(audio_path and os.path.isfile(audio_path))
        if has_audio:
            cmd += ["-i", audio_path]

        if use_transparent:
            cmd += [
                "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p",
                "-auto-alt-ref", "0",
                "-crf", "18",
                "-b:v", "0",
            ]
        else:
            cmd += [
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-crf", "18",
                "-preset", "fast",
            ]

        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]

        cmd.append(output_path)

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

        # Drain stderr in background to prevent Windows pipe deadlock
        stderr_lines = []

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line.decode(errors="replace"))
            proc.stderr.close()

        drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
        drain_thread.start()

        try:
            for i in range(total_frames):
                if cancel_flag and cancel_flag():
                    proc.stdin.close()
                    proc.kill()
                    return False, "Renderização cancelada."

                frame = self.render_frame_bytes(i / self.fps)
                proc.stdin.write(frame)

                if progress_callback and i % 15 == 0:
                    progress_callback(i / total_frames)

            proc.stdin.close()

        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.kill()
            drain_thread.join(timeout=3)
            return False, f"Erro ao escrever frames: {e}"

        proc.wait()
        drain_thread.join(timeout=10)

        if progress_callback:
            progress_callback(1.0)

        if proc.returncode == 0:
            return True, output_path
        else:
            err = "".join(stderr_lines[-30:])
            return False, f"FFmpeg erro (código {proc.returncode}):\n{err}"


# ─────────────────────────────────────────────
#  GUI — Main App
# ─────────────────────────────────────────────

DARK = {
    "bg": "#1a1a2e",
    "surface": "#16213e",
    "surface2": "#0f3460",
    "accent": "#e94560",
    "accent2": "#533483",
    "text": "#eaeaea",
    "text_dim": "#8888aa",
    "border": "#2a2a4a",
    "success": "#4caf50",
    "warn": "#ff9800",
    "entry_bg": "#0d1b2a",
}


class StyledButton(tk.Button):
    def __init__(self, parent, text, command=None, style="primary", **kwargs):
        colors = {
            "primary": (DARK["accent"], "#fff"),
            "secondary": (DARK["surface2"], DARK["text"]),
            "danger": ("#7a1e2e", "#ff8a8a"),
            "success": ("#1b5e20", "#a5d6a7"),
        }
        bg, fg = colors.get(style, colors["primary"])
        # Use caller-supplied padx/pady if provided, else defaults
        kwargs.setdefault("padx", 14)
        kwargs.setdefault("pady", 8)
        kwargs.setdefault("font", ("Segoe UI", 10, "bold"))
        super().__init__(
            parent, text=text, command=command,
            bg=bg, fg=fg, activebackground=DARK["accent2"],
            activeforeground="#fff", relief="flat",
            bd=0, cursor="hand2",
            **kwargs
        )
        self.bind("<Enter>", lambda e: self.config(bg=self._lighten(bg)))
        self.bind("<Leave>", lambda e: self.config(bg=bg))

    def _lighten(self, hex_color):
        try:
            r, g, b = hex_to_rgb(hex_color)
            h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
            v = min(1.0, v + 0.15)
            r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
            return f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"
        except Exception:
            return hex_color


class StropheEditor(tk.Toplevel):
    """Dialog to add/edit a strophe."""
    def __init__(self, parent, strophe: Optional[Strophe] = None, on_save=None):
        super().__init__(parent)
        self.on_save = on_save
        self.strophe = strophe
        self.result = None

        self.title("Editar Estrofe" if strophe else "Nova Estrofe")
        self.configure(bg=DARK["bg"])
        self.resizable(False, False)
        self.grab_set()

        self._build()
        if strophe:
            self._fill(strophe)

        self.transient(parent)
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _lbl(self, parent, text):
        return tk.Label(parent, text=text, bg=DARK["bg"], fg=DARK["text_dim"],
                        font=("Segoe UI", 9), anchor="w")

    def _entry(self, parent, width=20):
        e = tk.Entry(parent, width=width, bg=DARK["entry_bg"], fg=DARK["text"],
                     insertbackground=DARK["text"], relief="flat",
                     font=("Segoe UI", 10), bd=4)
        return e

    def _build(self):
        pad = {"padx": 16, "pady": 6}

        # Times row
        time_frame = tk.Frame(self, bg=DARK["bg"])
        time_frame.pack(fill="x", **pad)

        tk.Label(time_frame, text="Início (MM:SS)", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", padx=(0, 16))
        self.start_var = tk.StringVar(value="00:00")
        e1 = self._entry(time_frame, width=10)
        e1.config(textvariable=self.start_var)
        e1.grid(row=1, column=0, padx=(0, 16))

        tk.Label(time_frame, text="Fim (MM:SS)", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9)).grid(row=0, column=1, sticky="w")
        self.end_var = tk.StringVar(value="00:10")
        e2 = self._entry(time_frame, width=10)
        e2.config(textvariable=self.end_var)
        e2.grid(row=1, column=1)

        # Tip
        tk.Label(self, text="Formatos aceitos: 00:05  |  00:05.50  |  1:30",
                 bg=DARK["bg"], fg=DARK["text_dim"], font=("Segoe UI", 8)).pack(
            anchor="w", padx=16)

        # Text area
        tk.Label(self, text="Letra da estrofe:", bg=DARK["bg"], fg=DARK["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(10, 2))

        text_frame = tk.Frame(self, bg=DARK["border"], padx=1, pady=1)
        text_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self.text_widget = tk.Text(text_frame, height=8, width=50,
                                   bg=DARK["entry_bg"], fg=DARK["text"],
                                   insertbackground=DARK["text"],
                                   font=("Segoe UI", 11), relief="flat",
                                   wrap="word", bd=8, spacing1=4, spacing2=2)
        self.text_widget.pack(fill="both", expand=True)

        # Buttons
        btn_frame = tk.Frame(self, bg=DARK["bg"])
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))

        StyledButton(btn_frame, "✕  Cancelar", command=self.destroy,
                     style="secondary").pack(side="right", padx=(8, 0))
        StyledButton(btn_frame, "✓  Salvar Estrofe", command=self._save).pack(side="right")

    def _fill(self, s: Strophe):
        def fmt(sec):
            m = int(sec) // 60
            sc = sec % 60
            return f"{m:02d}:{sc:05.2f}"
        self.start_var.set(fmt(s.start_time))
        self.end_var.set(fmt(s.end_time))
        self.text_widget.delete("1.0", "end")
        self.text_widget.insert("1.0", s.text)

    def _save(self):
        try:
            start = parse_time(self.start_var.get())
            end = parse_time(self.end_var.get())
        except ValueError as e:
            messagebox.showerror("Erro de tempo", str(e), parent=self)
            return

        if end <= start:
            messagebox.showerror("Erro", "O fim deve ser após o início.", parent=self)
            return

        text = self.text_widget.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showerror("Erro", "A letra não pode estar vazia.", parent=self)
            return

        s_id = self.strophe.id if self.strophe else 0
        result = Strophe(id=s_id, start_time=start, end_time=end, text=text)
        if self.on_save:
            self.on_save(result)
        self.destroy()


class SettingsPanel(tk.Frame):
    def __init__(self, parent, project: Project, on_change=None):
        super().__init__(parent, bg=DARK["surface"])
        self.project = project
        self.on_change = on_change
        self._build()

    def _section(self, text):
        parent = getattr(self, "_inner", self)
        f = tk.Frame(parent, bg=DARK["surface"])
        f.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(f, text=text.upper(), bg=DARK["surface"],
                 fg=DARK["accent"], font=("Segoe UI", 8, "bold")).pack(anchor="w")
        sep = tk.Frame(f, bg=DARK["border"], height=1)
        sep.pack(fill="x", pady=(2, 0))
        return f

    def _row(self, parent, label, widget_factory):
        row = tk.Frame(parent, bg=DARK["surface"])
        row.pack(fill="x", padx=12, pady=3)
        tk.Label(row, text=label, bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        w = widget_factory(row)
        w.pack(side="left", fill="x", expand=True)
        return w

    def _entry(self, parent, var, width=None):
        kw = {"textvariable": var, "bg": DARK["entry_bg"], "fg": DARK["text"],
              "insertbackground": DARK["text"], "relief": "flat",
              "font": ("Segoe UI", 10), "bd": 4}
        if width:
            kw["width"] = width
        e = tk.Entry(parent, **kw)
        e.bind("<FocusOut>", lambda _: self._notify())
        return e

    def _spin(self, parent, var, from_, to, width=7):
        s = tk.Spinbox(parent, textvariable=var, from_=from_, to=to, width=width,
                       bg=DARK["entry_bg"], fg=DARK["text"], buttonbackground=DARK["surface2"],
                       insertbackground=DARK["text"], relief="flat", font=("Segoe UI", 10),
                       command=self._notify)
        s.bind("<FocusOut>", lambda _: self._notify())
        return s

    def _color_btn(self, parent, var):
        from tkinter import colorchooser
        btn = tk.Button(parent, bg=var.get(), width=4, relief="flat", bd=0,
                        cursor="hand2")
        def pick():
            c = colorchooser.askcolor(color=var.get(), parent=self)[1]
            if c:
                var.set(c)
                btn.config(bg=c)
                self._notify()
        btn.config(command=pick)
        return btn

    def _build(self):
        p = self.project

        # Wrap everything in a scrollable canvas so the panel never clips on small screens
        outer = tk.Canvas(self, bg=DARK["surface"], highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=outer.yview)
        outer.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        outer.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(outer, bg=DARK["surface"])
        win_id = outer.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>", lambda e: outer.itemconfig(win_id, width=e.width))
        for widget in (outer, inner):
            widget.bind("<MouseWheel>", lambda e: outer.yview_scroll(-1*(e.delta//120), "units"))
            widget.bind("<Button-4>",   lambda e: outer.yview_scroll(-1, "units"))
            widget.bind("<Button-5>",   lambda e: outer.yview_scroll( 1, "units"))
        self._inner = inner  # all sub-widgets go here

        # ── Song settings ──────────────────────────────────
        self._section("🎵 Música")

        self.title_var = tk.StringVar(value=p.title)
        self._row(inner, "Título", lambda f: self._entry(f, self.title_var))

        # Audio
        af = tk.Frame(inner, bg=DARK["surface"])
        af.pack(fill="x", padx=12, pady=3)
        tk.Label(af, text="Áudio (opcional)", bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        self.audio_var = tk.StringVar(value=p.audio_file)
        e = tk.Entry(af, textvariable=self.audio_var, bg=DARK["entry_bg"], fg=DARK["text"],
                     insertbackground=DARK["text"], relief="flat", font=("Segoe UI", 9), bd=4)
        e.pack(side="left", fill="x", expand=True, padx=(0, 6))
        StyledButton(af, "...", command=self._pick_audio, style="secondary",
                     padx=8, pady=4).pack(side="left")

        # ── Typography ─────────────────────────────────────
        self._section("✏️ Tipografia")

        self.title_font_var = tk.StringVar(value=p.title_font)
        self._row(inner, "Fonte do título", lambda f: self._entry(f, self.title_font_var))

        self.title_size_var = tk.IntVar(value=p.title_size)
        self._row(inner, "Tamanho título", lambda f: self._spin(f, self.title_size_var, 20, 200))

        self.lyric_font_var = tk.StringVar(value=p.lyric_font)
        self._row(inner, "Fonte da letra", lambda f: self._entry(f, self.lyric_font_var))

        self.lyric_size_var = tk.IntVar(value=p.lyric_size)
        self._row(inner, "Tamanho letra", lambda f: self._spin(f, self.lyric_size_var, 16, 160))

        self.line_spacing_var = tk.IntVar(value=p.line_spacing)
        self._row(inner, "Espaç. linhas", lambda f: self._spin(f, self.line_spacing_var, 0, 80))

        # ── Cores ──────────────────────────────────────────
        self._section("🎨 Cores")

        self.title_color_var = tk.StringVar(value=p.title_color)
        self._row(inner, "Cor do título", lambda f: self._color_btn(f, self.title_color_var))

        self.text_color_var = tk.StringVar(value=p.text_color)
        self._row(inner, "Cor da letra",  lambda f: self._color_btn(f, self.text_color_var))

        # ── Fundo ──────────────────────────────────────────
        self._section("🖼️ Fundo")

        # Transparent toggle
        tr_row = tk.Frame(inner, bg=DARK["surface"])
        tr_row.pack(fill="x", padx=12, pady=3)
        tk.Label(tr_row, text="Fundo transparente", bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        self.transparent_var = tk.BooleanVar(value=p.transparent_bg)
        chk = tk.Checkbutton(tr_row, variable=self.transparent_var,
                              bg=DARK["surface"], fg=DARK["text"],
                              activebackground=DARK["surface"], selectcolor=DARK["entry_bg"],
                              command=self._on_transparent_toggle)
        chk.pack(side="left")
        tk.Label(tr_row, text="(WebM com alpha — para overlay)", bg=DARK["surface"],
                 fg=DARK["text_dim"], font=("Segoe UI", 8)).pack(side="left", padx=(6,0))

        # Solid bg color (shown only when not transparent)
        self._bg_color_row = tk.Frame(inner, bg=DARK["surface"])
        self._bg_color_row.pack(fill="x", padx=12, pady=3)
        tk.Label(self._bg_color_row, text="Cor de fundo", bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        self.bg_color_var = tk.StringVar(value=p.bg_color)
        self._color_btn(self._bg_color_row, self.bg_color_var).pack(side="left")

        # Background image
        bg_row = tk.Frame(inner, bg=DARK["surface"])
        bg_row.pack(fill="x", padx=12, pady=3)
        tk.Label(bg_row, text="Imagem de fundo", bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        self.bg_image_var = tk.StringVar(value=p.bg_image)
        ei = tk.Entry(bg_row, textvariable=self.bg_image_var, bg=DARK["entry_bg"],
                      fg=DARK["text"], insertbackground=DARK["text"],
                      relief="flat", font=("Segoe UI", 9), bd=4)
        ei.pack(side="left", fill="x", expand=True, padx=(0, 6))
        StyledButton(bg_row, "...", command=self._pick_bg_image, style="secondary",
                     padx=8, pady=4).pack(side="left")
        tk.Label(inner, text="Imagem substitui cor de fundo e desativa transparência.",
                 bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12)

        # ── Vídeo ──────────────────────────────────────────
        self._section("🎬 Vídeo")

        self.fade_var = tk.DoubleVar(value=p.fade_duration)
        self._row(inner, "Fade (seg)", lambda f: self._spin(f, self.fade_var, 0.1, 5.0, 7))

        self.fps_var = tk.IntVar(value=p.fps)
        self._row(inner, "FPS", lambda f: self._spin(f, self.fps_var, 10, 60))

        res_frame = tk.Frame(inner, bg=DARK["surface"])
        res_frame.pack(fill="x", padx=12, pady=3)
        tk.Label(res_frame, text="Resolução", bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        self.res_var = tk.StringVar()
        res_menu = ttk.Combobox(res_frame, textvariable=self.res_var,
                                values=["1920x1080", "1280x720", "3840x2160"],
                                state="readonly", width=12, font=("Segoe UI", 10))
        res_menu.set(f"{p.video_width}x{p.video_height}")
        res_menu.pack(side="left")
        res_menu.bind("<<ComboboxSelected>>", lambda _: self._notify())

        self._on_transparent_toggle()  # set initial visibility

        # Apply button
        StyledButton(inner, "  Aplicar Configurações", command=self._apply,
                     style="secondary").pack(padx=12, pady=(16, 8), fill="x")

    def _on_transparent_toggle(self):
        """Show/hide bg color row based on transparency checkbox."""
        if self.transparent_var.get():
            self._bg_color_row.pack_forget()
        else:
            self._bg_color_row.pack(fill="x", padx=12, pady=3,
                                    before=self._bg_color_row.master.winfo_children()[
                                        list(self._bg_color_row.master.winfo_children()).index(self._bg_color_row)
                                    ] if self._bg_color_row.winfo_ismapped() else self._bg_color_row)

    def _pick_audio(self):
        path = filedialog.askopenfilename(
            filetypes=[("Áudio", "*.mp3 *.wav *.ogg *.aac *.m4a"), ("Todos", "*.*")]
        )
        if path:
            self.audio_var.set(path)
            self._notify()

    def _pick_bg_image(self):
        path = filedialog.askopenfilename(
            filetypes=[("Imagem", "*.png *.jpg *.jpeg *.bmp *.webp"), ("Todos", "*.*")]
        )
        if path:
            self.bg_image_var.set(path)
            # Having a bg image disables transparency
            self.transparent_var.set(False)
            self._on_transparent_toggle()
            self._notify()

    def _apply(self):
        p = self.project
        p.title        = self.title_var.get().strip() or "Título"
        p.title_font   = self.title_font_var.get().strip() or "mvboli"
        p.title_size   = int(self.title_size_var.get())
        p.lyric_font   = self.lyric_font_var.get().strip() or "mvboli"
        p.lyric_size   = int(self.lyric_size_var.get())
        p.line_spacing = int(self.line_spacing_var.get())
        p.fade_duration = float(self.fade_var.get())
        p.fps          = int(self.fps_var.get())
        p.title_color  = self.title_color_var.get()
        p.text_color   = self.text_color_var.get()
        p.bg_color     = self.bg_color_var.get()
        p.bg_image     = self.bg_image_var.get().strip()
        p.transparent_bg = self.transparent_var.get()
        p.audio_file   = self.audio_var.get().strip()

        # If bg image is set, disable transparency
        if p.bg_image and os.path.isfile(p.bg_image):
            p.transparent_bg = False

        res = self.res_var.get()
        if "x" in res:
            w, h = res.split("x")
            p.video_width  = int(w)
            p.video_height = int(h)

        if self.on_change:
            self.on_change()

    def _notify(self):
        pass  # apply only on button click


class StropheList(tk.Frame):
    def __init__(self, parent, project: Project, on_change=None):
        super().__init__(parent, bg=DARK["bg"])
        self.project = project
        self.on_change = on_change
        self._build()

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=DARK["bg"])
        hdr.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(hdr, text="ESTROFES", bg=DARK["bg"], fg=DARK["accent"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")

        StyledButton(hdr, "+  Nova Estrofe", command=self._add_strophe,
                     style="primary", padx=10, pady=5).pack(side="right")
        StyledButton(hdr, "📋 Colar Bloco", command=self._paste_block,
                     style="secondary", padx=10, pady=5).pack(side="right", padx=(0, 6))

        # Canvas + scrollbar for strophe cards
        container = tk.Frame(self, bg=DARK["bg"])
        container.pack(fill="both", expand=True, padx=4)

        self.canvas = tk.Canvas(container, bg=DARK["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=DARK["bg"])

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mousewheel
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1*(e.delta//120), "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self.refresh()

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def refresh(self):
        for w in self.scrollable_frame.winfo_children():
            w.destroy()

        strophes = sorted(self.project.strophes, key=lambda s: s.start_time)

        if not strophes:
            tk.Label(self.scrollable_frame,
                     text="Nenhuma estrofe.\nClique em '+ Nova Estrofe' para adicionar.",
                     bg=DARK["bg"], fg=DARK["text_dim"],
                     font=("Segoe UI", 11), justify="center").pack(pady=40)
            return

        for i, s in enumerate(strophes):
            self._strophe_card(self.scrollable_frame, s, i)

    def _strophe_card(self, parent, s: Strophe, idx: int):
        card = tk.Frame(parent, bg=DARK["surface"], pady=0)
        card.pack(fill="x", padx=8, pady=4)

        # Color accent strip
        accent_color = DARK["accent"] if idx % 2 == 0 else DARK["accent2"]
        strip = tk.Frame(card, bg=accent_color, width=4)
        strip.pack(side="left", fill="y")

        inner = tk.Frame(card, bg=DARK["surface"])
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=8)

        # Top row: time and buttons
        top = tk.Frame(inner, bg=DARK["surface"])
        top.pack(fill="x")

        def fmt(sec):
            m = int(sec) // 60
            sc = int(sec) % 60
            cs = int((sec % 1) * 100)
            return f"{m:02d}:{sc:02d}.{cs:02d}"

        time_str = f"⏱  {fmt(s.start_time)}  →  {fmt(s.end_time)}   ({s.end_time - s.start_time:.1f}s)"
        tk.Label(top, text=time_str, bg=DARK["surface"], fg=DARK["accent"],
                 font=("Courier New", 10, "bold")).pack(side="left")

        StyledButton(top, "✎", command=lambda sid=s.id: self._edit(sid),
                     style="secondary", padx=6, pady=2,
                     font=("Segoe UI", 9)).pack(side="right", padx=(4, 0))
        StyledButton(top, "✕", command=lambda sid=s.id: self._delete(sid),
                     style="danger", padx=6, pady=2,
                     font=("Segoe UI", 9)).pack(side="right")

        # Lyrics preview
        preview = s.text.strip()[:120] + ("…" if len(s.text.strip()) > 120 else "")
        tk.Label(inner, text=preview, bg=DARK["surface"], fg=DARK["text"],
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=600).pack(fill="x", pady=(4, 0))

    def _add_strophe(self):
        next_start = 0.0
        if self.project.strophes:
            next_start = max(s.end_time for s in self.project.strophes)

        def on_save(s: Strophe):
            s.id = max((x.id for x in self.project.strophes), default=0) + 1
            self.project.strophes.append(s)
            self.refresh()
            if self.on_change:
                self.on_change()

        StropheEditor(self, on_save=on_save)

    def _edit(self, sid: int):
        s = next((x for x in self.project.strophes if x.id == sid), None)
        if not s:
            return

        def on_save(new_s: Strophe):
            new_s.id = sid
            idx = next(i for i, x in enumerate(self.project.strophes) if x.id == sid)
            self.project.strophes[idx] = new_s
            self.refresh()
            if self.on_change:
                self.on_change()

        StropheEditor(self, strophe=s, on_save=on_save)

    def _delete(self, sid: int):
        if messagebox.askyesno("Confirmar", "Remover esta estrofe?"):
            self.project.strophes = [s for s in self.project.strophes if s.id != sid]
            self.refresh()
            if self.on_change:
                self.on_change()

    def _paste_block(self):
        """Quick paste dialog: paste multiple strophes at once."""
        PasteBlockDialog(self, self.project, on_done=lambda: (self.refresh(),
                                                               self.on_change() if self.on_change else None))


class PasteBlockDialog(tk.Toplevel):
    """
    Paste a full lyrics block:

    00:05 - 00:15
    Letra aqui
    mais letra

    00:15 - 00:25
    Outra estrofe
    """
    def __init__(self, parent, project: Project, on_done=None):
        super().__init__(parent)
        self.project = project
        self.on_done = on_done
        self.title("Colar Bloco de Letras")
        self.configure(bg=DARK["bg"])
        self.resizable(True, True)
        self.grab_set()
        self._build()
        self.geometry("640x580")
        self.transient(parent)

    def _build(self):
        tk.Label(self, text="Cole as letras no formato abaixo:",
                 bg=DARK["bg"], fg=DARK["text"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=16, pady=(14, 4))

        example = (
            "Formatos aceitos para cada estrofe:\n\n"
            "  00:05 - 00:15\n"
            "  Letra da estrofe\n"
            "  mais letras\n\n"
            "  00:20 - 00:30\n"
            "  Próxima estrofe\n\n"
            "─────────────────────────────────────\n"
            "Separe estrofes por linha em branco."
        )
        tk.Label(self, text=example, bg=DARK["surface"], fg=DARK["text_dim"],
                 font=("Courier New", 9), justify="left", padx=12, pady=8).pack(
            fill="x", padx=16, pady=(0, 8))

        tf = tk.Frame(self, bg=DARK["border"], padx=1, pady=1)
        tf.pack(fill="both", expand=True, padx=16)

        self.text = tk.Text(tf, bg=DARK["entry_bg"], fg=DARK["text"],
                            insertbackground=DARK["text"],
                            font=("Courier New", 11), relief="flat", bd=8,
                            spacing1=3, spacing2=1, wrap="word")
        self.text.pack(fill="both", expand=True)

        bf = tk.Frame(self, bg=DARK["bg"])
        bf.pack(fill="x", padx=16, pady=12)
        StyledButton(bf, "✕  Cancelar", command=self.destroy, style="secondary").pack(side="right", padx=(8, 0))
        StyledButton(bf, "✓  Importar Estrofes", command=self._import).pack(side="right")

    def _import(self):
        raw = self.text.get("1.0", "end-1c").strip()
        if not raw:
            messagebox.showerror("Erro", "O campo está vazio.", parent=self)
            return

        blocks = re.split(r"\n\s*\n", raw)
        imported = 0
        errors = []
        next_id = max((s.id for s in self.project.strophes), default=0) + 1

        time_pattern = re.compile(
            r"^(\d{1,2}:\d{2}(?:\.\d+)?)\s*[-–—]\s*(\d{1,2}:\d{2}(?:\.\d+)?)"
        )

        for block in blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.split("\n")
            first = lines[0].strip()
            m = time_pattern.match(first)
            if not m:
                errors.append(f"Linha sem tempo reconhecido: '{first[:40]}'")
                continue
            try:
                start = parse_time(m.group(1))
                end = parse_time(m.group(2))
            except ValueError as e:
                errors.append(str(e))
                continue

            text_lines = [l for l in lines[1:] if l.strip()]
            if not text_lines:
                errors.append(f"Estrofe em {m.group(0)}: sem letra.")
                continue

            text = "\n".join(l.strip() for l in text_lines)
            self.project.strophes.append(Strophe(id=next_id, start_time=start, end_time=end, text=text))
            next_id += 1
            imported += 1

        if errors:
            msg = f"Importadas: {imported}\n\nErros ({len(errors)}):\n" + "\n".join(errors[:5])
            messagebox.showwarning("Importação parcial", msg, parent=self)
        else:
            messagebox.showinfo("Sucesso", f"{imported} estrofe(s) importada(s)!", parent=self)

        if self.on_done:
            self.on_done()
        self.destroy()


class RenderDialog(tk.Toplevel):
    def __init__(self, parent, project: Project):
        super().__init__(parent)
        self.project = project
        self.cancelled = False
        self.title("Renderizar Vídeo")
        self.configure(bg=DARK["bg"])
        self.resizable(False, False)
        self.grab_set()
        self._build()
        self.transient(parent)
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _build(self):
        tk.Label(self, text="🎬  Renderizar Vídeo", bg=DARK["bg"],
                 fg=DARK["text"], font=("Segoe UI", 13, "bold")).pack(pady=(20, 8), padx=24)

        # Output path
        pf = tk.Frame(self, bg=DARK["bg"])
        pf.pack(fill="x", padx=24, pady=8)
        tk.Label(pf, text="Salvar em:", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9)).pack(anchor="w")
        row = tk.Frame(pf, bg=DARK["bg"])
        row.pack(fill="x", pady=(4, 0))

        default_out = self.project.output_file or os.path.join(
            os.path.expanduser("~"), f"{self.project.title or 'video'}.mp4"
        )
        self.out_var = tk.StringVar(value=default_out)
        e = tk.Entry(row, textvariable=self.out_var, bg=DARK["entry_bg"], fg=DARK["text"],
                     insertbackground=DARK["text"], relief="flat", font=("Segoe UI", 10), bd=4)
        e.pack(side="left", fill="x", expand=True, padx=(0, 8))
        StyledButton(row, "...", command=self._pick_out, style="secondary",
                     padx=8, pady=4).pack(side="left")

        # Format info
        tk.Label(self,
                 text=".mp4 → com fundo  |  .webm → com transparência (gerado automaticamente se fundo transparente)",
                 bg=DARK["bg"], fg=DARK["text_dim"], font=("Segoe UI", 8)).pack(padx=24)

        # Progress
        tk.Label(self, text="", bg=DARK["bg"], height=1).pack()
        self.progress_label = tk.Label(self, text="Pronto para renderizar.",
                                       bg=DARK["bg"], fg=DARK["text_dim"],
                                       font=("Segoe UI", 9))
        self.progress_label.pack(padx=24)

        self.progress_bar = ttk.Progressbar(self, length=400, mode="determinate")
        self.progress_bar.pack(padx=24, pady=8)

        self.status_label = tk.Label(self, text="", bg=DARK["bg"],
                                     fg=DARK["text_dim"], font=("Segoe UI", 8))
        self.status_label.pack(padx=24)

        # Buttons
        bf = tk.Frame(self, bg=DARK["bg"])
        bf.pack(fill="x", padx=24, pady=(16, 20))

        self.cancel_btn = StyledButton(bf, "✕  Cancelar", command=self._cancel,
                                       style="secondary")
        self.cancel_btn.pack(side="right", padx=(8, 0))

        self.render_btn = StyledButton(bf, "▶  Iniciar Renderização",
                                       command=self._start_render)
        self.render_btn.pack(side="right")

    def _pick_out(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4"), ("WebM transparente", "*.webm"),
                       ("Todos", "*.*")],
            initialfile=f"{self.project.title or 'video'}.mp4"
        )
        if path:
            self.out_var.set(path)
            self.project.output_file = path

    def _cancel(self):
        self.cancelled = True
        self.destroy()

    def _start_render(self):
        out = self.out_var.get().strip()
        if not out:
            messagebox.showerror("Erro", "Escolha onde salvar o vídeo.", parent=self)
            return

        if not self.project.strophes:
            messagebox.showerror("Erro", "Adicione ao menos uma estrofe.", parent=self)
            return

        self.project.output_file = out
        self.render_btn.config(state="disabled")
        self.cancel_btn.config(text="⏹  Parar")
        self.cancelled = False

        renderer = FrameRenderer(self.project)

        # IMPORTANT: never touch tkinter widgets from background thread on Windows.
        # Use after() to post updates back to the main thread.
        def progress(p):
            pct = int(p * 100)
            self.after(0, lambda v=pct: self._set_progress(v))

        def run():
            ok, msg = renderer.render_video(
                out,
                audio_path=self.project.audio_file,
                progress_callback=progress,
                cancel_flag=lambda: self.cancelled,
            )
            self.after(0, lambda: self._on_done(ok, msg))

        threading.Thread(target=run, daemon=True).start()
        self.progress_label.config(text="Iniciando…")

    def _set_progress(self, pct: int):
        """Thread-safe progress update — called only on main thread."""
        try:
            self.progress_bar["value"] = pct
            self.progress_label.config(text=f"Renderizando… {pct}%")
        except Exception:
            pass

    def _on_done(self, ok: bool, msg: str):
        if ok:
            self.progress_label.config(text="✅  Renderização concluída!", fg=DARK["success"])
            self.render_btn.config(state="normal")
            if messagebox.askyesno("Concluído", f"Vídeo salvo em:\n{msg}\n\nAbrir pasta?", parent=self):
                folder = os.path.dirname(msg)
                if sys.platform == "win32":
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.run(["open", folder])
                else:
                    subprocess.run(["xdg-open", folder])
            self.destroy()
        else:
            self.progress_label.config(text="❌  Erro na renderização", fg=DARK["accent"])
            self.render_btn.config(state="normal")
            messagebox.showerror("Erro", msg, parent=self)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LyricRenderer")
        self.configure(bg=DARK["bg"])
        self.geometry("1280x800")
        self.minsize(900, 600)

        self.project = Project()
        self._current_file = None

        self._setup_styles()
        self._build_menu()
        self._build_ui()

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TScrollbar", background=DARK["surface"], troughcolor=DARK["bg"],
                        bordercolor=DARK["bg"], arrowcolor=DARK["text_dim"])
        style.configure("Horizontal.TProgressbar", background=DARK["accent"],
                        troughcolor=DARK["surface"])
        style.configure("TCombobox", fieldbackground=DARK["entry_bg"],
                        background=DARK["surface"], foreground=DARK["text"],
                        arrowcolor=DARK["text"])

    def _build_menu(self):
        menubar = tk.Menu(self, bg=DARK["surface"], fg=DARK["text"],
                          activebackground=DARK["accent"], activeforeground="#fff",
                          relief="flat", bd=0)
        self.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0, bg=DARK["surface"], fg=DARK["text"],
                            activebackground=DARK["accent"], activeforeground="#fff")
        file_menu.add_command(label="Novo projeto", command=self._new_project)
        file_menu.add_command(label="Abrir projeto…", command=self._open_project)
        file_menu.add_command(label="Salvar projeto", command=self._save_project, accelerator="Ctrl+S")
        file_menu.add_command(label="Salvar como…", command=self._save_project_as)
        file_menu.add_separator()
        file_menu.add_command(label="Sair", command=self.quit)
        menubar.add_cascade(label="Arquivo", menu=file_menu)

        render_menu = tk.Menu(menubar, tearoff=0, bg=DARK["surface"], fg=DARK["text"],
                              activebackground=DARK["accent"], activeforeground="#fff")
        render_menu.add_command(label="Renderizar vídeo…", command=self._render, accelerator="Ctrl+R")
        menubar.add_cascade(label="Renderizar", menu=render_menu)

        self.bind_all("<Control-s>", lambda e: self._save_project())
        self.bind_all("<Control-r>", lambda e: self._render())

    def _build_ui(self):
        # Title bar area
        topbar = tk.Frame(self, bg=DARK["surface"], height=56)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        tk.Label(topbar, text="♪  LyricRenderer", bg=DARK["surface"],
                 fg=DARK["accent"], font=("Segoe UI", 16, "bold")).pack(side="left", padx=20, pady=12)

        StyledButton(topbar, "▶  Renderizar", command=self._render,
                     style="primary").pack(side="right", padx=16, pady=10)
        StyledButton(topbar, "💾  Salvar", command=self._save_project,
                     style="secondary").pack(side="right", pady=10)

        # Main pane
        paned = tk.PanedWindow(self, orient="horizontal", bg=DARK["bg"],
                                sashwidth=6, sashpad=0, sashrelief="flat",
                                handlepad=80, handlesize=8)
        paned.pack(fill="both", expand=True)

        # Left: settings
        left_scroll_frame = tk.Frame(paned, bg=DARK["surface"], width=300)
        paned.add(left_scroll_frame, minsize=240)

        self.settings = SettingsPanel(left_scroll_frame, self.project,
                                      on_change=self._on_project_change)
        self.settings.pack(fill="both", expand=True)

        # Right: strophe list
        right_frame = tk.Frame(paned, bg=DARK["bg"])
        paned.add(right_frame, minsize=400)

        self.strophe_list = StropheList(right_frame, self.project,
                                         on_change=self._on_project_change)
        self.strophe_list.pack(fill="both", expand=True)

        # Status bar
        self.statusbar = tk.Label(self, text="Pronto.", bg=DARK["surface"],
                                  fg=DARK["text_dim"], font=("Segoe UI", 8),
                                  anchor="w", padx=12, pady=4)
        self.statusbar.pack(fill="x", side="bottom")

    def _on_project_change(self):
        count = len(self.project.strophes)
        self.statusbar.config(
            text=f"Projeto: {self.project.title}  |  {count} estrofe(s)  |  "
                 f"{'Salvo' if self._current_file else 'Não salvo'}")

    # ── File operations ───────────────────────

    def _new_project(self):
        if messagebox.askyesno("Novo projeto", "Descartar projeto atual e criar novo?"):
            self.project = Project()
            self._current_file = None
            self.settings.destroy()
            self.strophe_list.destroy()
            self._build_ui_panels()

    def _build_ui_panels(self):
        # Re-instantiate panels after new project
        for w in self.winfo_children():
            if isinstance(w, (SettingsPanel, StropheList)):
                w.destroy()

    def _open_project(self):
        path = filedialog.askopenfilename(filetypes=[("LyricRenderer", "*.lyr"),
                                                     ("JSON", "*.json"), ("Todos", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.project = Project.from_dict(data)
            self._current_file = path
            # Refresh UI
            self.settings.project = self.project
            self.settings._apply()
            self.strophe_list.project = self.project
            self.strophe_list.refresh()
            self._on_project_change()
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível abrir:\n{e}")

    def _save_project(self):
        if not self._current_file:
            self._save_project_as()
            return
        self._do_save(self._current_file)

    def _save_project_as(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".lyr",
            filetypes=[("LyricRenderer", "*.lyr"), ("JSON", "*.json")],
            initialfile=f"{self.project.title}.lyr"
        )
        if path:
            self._current_file = path
            self._do_save(path)

    def _do_save(self, path: str):
        self.settings._apply()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.project.to_dict(), f, ensure_ascii=False, indent=2)
            self._on_project_change()
            self.statusbar.config(text=f"Salvo em {path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível salvar:\n{e}")

    def _render(self):
        self.settings._apply()
        if not self.project.strophes:
            messagebox.showwarning("Aviso", "Adicione ao menos uma estrofe antes de renderizar.")
            return
        RenderDialog(self, self.project)


def main():
    if not HAS_PIL:
        print("Pillow não encontrado. Instale com: pip install Pillow")
        sys.exit(1)

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
