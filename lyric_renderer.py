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
import queue
import re
import struct
import zlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional
import colorsys

import auto_lyrics

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
        return format_time(seconds)

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
        strophes_data = d.get("strophes", [])
        # Remove keys not in dataclass (forward compat)
        valid = {f.name for f in Project.__dataclass_fields__.values()} - {"strophes"}
        proj = cls(**{k: v for k, v in d.items() if k in valid})
        s_valid = {f.name for f in Strophe.__dataclass_fields__.values()}
        proj.strophes = [Strophe(**{k: v for k, v in s.items() if k in s_valid})
                         for s in strophes_data]
        return proj


def format_time(seconds: float) -> str:
    """Format seconds as MM:SS.cc (rounded to centiseconds)."""
    total_cs = int(round(seconds * 100))
    m, rest = divmod(total_cs, 6000)
    s, cs = divmod(rest, 100)
    return f"{m:02d}:{s:02d}.{cs:02d}"


TIME_MASK_DIGITS = 4                # MM:SS → up to 99:59
TIME_MASK_MAX_SECONDS = 99 * 60 + 59


def mask_time_digits(digits: str) -> str:
    """Bank-style mask: typed digits fill MM:SS from the right.
    '' → 00:00, '5' → 00:05, '130' → 01:30, '1000' → 10:00."""
    d = digits[-TIME_MASK_DIGITS:].rjust(TIME_MASK_DIGITS, "0")
    return f"{d[:2]}:{d[2:]}"


def time_digits_to_seconds(digits: str) -> int:
    """'130' → 90. Seconds above 59 still count ('190' → 150)."""
    d = digits[-TIME_MASK_DIGITS:].rjust(TIME_MASK_DIGITS, "0")
    return int(d[:2]) * 60 + int(d[2:])


def seconds_to_time_digits(seconds: float) -> str:
    """90 → '130' (whole seconds, clamped to 00:00–99:59)."""
    total = min(max(int(seconds), 0), TIME_MASK_MAX_SECONDS)
    m, s = divmod(total, 60)
    return f"{m:02d}{s:02d}".lstrip("0")


def safe_filename(name: str) -> str:
    """Strip characters that are invalid in Windows/Unix file names."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return cleaned or "video"


_TIME_RE = r"\d{1,2}(?::\d{2}){1,2}(?:[.,]\d+)?"
_BLOCK_HEADER = re.compile(rf"^({_TIME_RE})\s*[-–—]\s*({_TIME_RE})\s*(.*)$")


def parse_lyrics_block(raw: str, next_id: int = 1):
    """
    Parse pasted lyrics in the "Colar Bloco" format into strophes.
    Returns (strophes, errors). Blocks are separated by blank lines and each
    one starts with "MM:SS - MM:SS" (text on the same line is kept).
    """
    strophes, errors = [], []
    for block in re.split(r"\n\s*\n", raw.strip()):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        first = lines[0].strip()
        m = _BLOCK_HEADER.match(first)
        if not m:
            errors.append(f"Linha sem tempo reconhecido: '{first[:40]}'")
            continue
        try:
            start = parse_time(m.group(1))
            end = parse_time(m.group(2))
        except ValueError as e:
            errors.append(str(e))
            continue
        header = f"{m.group(1)} - {m.group(2)}"
        if end <= start:
            errors.append(f"Estrofe em {header}: o fim deve ser após o início.")
            continue

        text_lines = [l.strip() for l in [m.group(3)] + lines[1:] if l.strip()]
        if not text_lines:
            errors.append(f"Estrofe em {header}: sem letra.")
            continue

        strophes.append(Strophe(id=next_id, start_time=start, end_time=end,
                                text="\n".join(text_lines)))
        next_id += 1
    return strophes, errors


def merge_with_next(strophes: List[Strophe], sid: int) -> List[Strophe]:
    """
    Join strophe `sid` with the one right after it (in time order): start of the
    first, end of the second, text of both. Returns the new list sorted by time;
    unchanged if `sid` is the last one or doesn't exist.
    """
    ordered = sorted(strophes, key=lambda s: s.start_time)
    idx = next((i for i, s in enumerate(ordered) if s.id == sid), None)
    if idx is None or idx + 1 >= len(ordered):
        return ordered
    a, b = ordered[idx], ordered[idx + 1]
    merged = Strophe(id=a.id,
                     start_time=min(a.start_time, b.start_time),
                     end_time=max(a.end_time, b.end_time),
                     text=a.text.strip() + "\n" + b.text.strip())
    return ordered[:idx] + [merged] + ordered[idx + 2:]


def next_strophe_times(strophes):
    """
    Default (start, end) for a new strophe: 1s after the latest-ending
    existing strophe, lasting 9s (0–10 → 11–20 → 21–30). First one is 0–10.
    """
    if not strophes:
        return 0.0, 10.0
    last_end = max(s.end_time for s in strophes)
    return last_end + 1, last_end + 10


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
    if len(hex_color) == 3:  # short form, e.g. "#fff"
        hex_color = "".join(c * 2 for c in hex_color)
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def find_font(font_name: str, size: int):
    """Try to find a system font by name, fallback to default."""
    if not HAS_PIL:
        return None

    # A full path to a font file wins over the fuzzy name search below
    if os.path.isfile(font_name):
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            pass

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


def _subprocess_flags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def probe_duration(path: str) -> float:
    """Media duration in seconds via ffprobe, or 0.0 if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_subprocess_flags(),
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def build_ffmpeg_cmd(output_path: str, width: int, height: int, fps: int,
                     transparent: bool, audio_path: str = ""):
    """
    Build the ffmpeg command for a raw-frame pipe on stdin.
    Returns (cmd, final_output_path).
    - Transparent → always WebM (VP9 + alpha)
    - .webm       → VP9 + Opus (WebM does not accept H.264/AAC)
    - otherwise   → H.264 + AAC
    """
    ext = Path(output_path).suffix.lower()
    if transparent and ext != ".webm":
        output_path = Path(output_path).with_suffix(".webm").as_posix()
        ext = ".webm"
    webm = ext == ".webm"

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "rgba" if transparent else "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "pipe:0",
    ]

    has_audio = bool(audio_path and os.path.isfile(audio_path))
    if has_audio:
        cmd += ["-i", audio_path]

    if webm:
        cmd += [
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p" if transparent else "yuv420p",
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
        if webm:
            cmd += ["-c:a", "libopus", "-b:a", "192k"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-shortest"]

    cmd.append(output_path)
    return cmd, output_path


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
        self.lyrics_end = max((s.end_time for s in self.strophes), default=0.0)

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
        a = self._alpha(t, t0, self.lyrics_end)
        if a <= 0:
            return
        color = (*self.title_rgb, int(a * 255))
        title_y = int(self.h * 0.08)
        _draw_text_centered(draw, self.proj.title, self.title_font, color, self.w, title_y)

    def active_strophe(self, t: float) -> Optional[Strophe]:
        """Strophe visible at time t (strictly inside, so back-to-back
        strophes don't hide each other on the boundary frame)."""
        for s in self.strophes:
            if s.start_time < t < s.end_time:
                return s
        return None

    def _draw_lyric(self, draw, t: float):
        active = self.active_strophe(t)
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
        Render all frames to video via ffmpeg pipe (see build_ffmpeg_cmd for
        the codec choice). The video lasts until the last strophe ends + 1.5s,
        or until the audio ends, whichever is longer.
        stderr drained in background thread to avoid Windows pipe deadlock.
        """
        if not self.strophes:
            return False, "Nenhuma estrofe adicionada."

        has_audio = bool(audio_path and os.path.isfile(audio_path))
        duration = self.lyrics_end + 1.5
        if has_audio:
            duration = max(duration, probe_duration(audio_path))
        total_frames = int(duration * self.fps)

        cmd, output_path = build_ffmpeg_cmd(
            output_path, self.w, self.h, self.fps, self._transparent,
            audio_path if has_audio else "")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_subprocess_flags(),
            )
        except FileNotFoundError:
            return False, ("FFmpeg não encontrado. Instale o FFmpeg e "
                           "garanta que ele está no PATH.")
        except OSError as e:
            if getattr(e, "winerror", None) == 4551:
                return False, ("O Windows bloqueou o FFmpeg (Smart App Control / "
                               "Controle de Aplicativo).\n\n"
                               "Veja em: Segurança do Windows → Controle de aplicativos "
                               "e navegador → Smart App Control.")
            return False, f"Não foi possível iniciar o FFmpeg:\n{e}"

        # Drain stderr in background to prevent Windows pipe deadlock
        stderr_lines = []

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line.decode(errors="replace"))
            proc.stderr.close()

        drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
        drain_thread.start()

        def _close_stdin():
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            for i in range(total_frames):
                if cancel_flag and cancel_flag():
                    _close_stdin()
                    proc.kill()
                    proc.wait()
                    return False, "Renderização cancelada."

                frame = self.render_frame_bytes(i / self.fps)
                proc.stdin.write(frame)

                if progress_callback and i % 15 == 0:
                    progress_callback(i / total_frames)

            _close_stdin()

        except OSError:
            # ffmpeg exited early (BrokenPipeError, or EINVAL on Windows).
            # Its stderr, reported below, says why.
            _close_stdin()
        except Exception as e:
            _close_stdin()
            proc.kill()
            proc.wait()
            drain_thread.join(timeout=3)
            return False, f"Erro ao renderizar frames: {e}"

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


def bind_wheel_scroll(canvas: tk.Canvas):
    """
    Scroll `canvas` with the mouse wheel while the pointer is over it or over
    anything inside it (cards, labels, buttons…). Wheel events go to the widget
    under the pointer, so a binding on the canvas alone only works on its empty
    areas — this listens app-wide and filters by widget path instead.
    """
    path = str(canvas)

    def on_wheel(e, step=None):
        w = str(e.widget)
        if w != path and not w.startswith(path + "."):
            return
        try:
            top, bottom = canvas.yview()
            if top <= 0 and bottom >= 1:
                return  # everything fits, nothing to scroll
            if step is None:
                step = -int(e.delta / 120) or (-1 if e.delta > 0 else 1)
            canvas.yview_scroll(step, "units")
        except tk.TclError:
            pass  # canvas destroyed

    canvas.bind_all("<MouseWheel>", on_wheel, add="+")
    canvas.bind_all("<Button-4>", lambda e: on_wheel(e, -1), add="+")  # Linux
    canvas.bind_all("<Button-5>", lambda e: on_wheel(e, 1), add="+")


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


class TimeEntry(tk.Frame):
    """
    MM:SS field with a bank-style mask: only digits are accepted and they
    fill from the right (1 → 00:01, 130 → 01:30). The first digit typed after
    focusing replaces the old value. ↑/↓ keys, the arrow buttons and the mouse
    wheel change it by 1s. Seconds ≥ 60 show in red and are normalized
    (01:90 → 02:30) when leaving the field or reading the value.
    """
    _PASS_KEYS = {"Tab", "ISO_Left_Tab", "Return", "KP_Enter", "Escape"}

    def __init__(self, parent, seconds: float = 0.0):
        super().__init__(parent, bg=DARK["bg"])
        self.var = tk.StringVar()
        self._digits = ""
        self._exact = None   # untouched value, keeps fractions from old projects
        self._fresh = False  # next digit replaces the value
        self._pending = set()  # after_idle jobs to cancel on destroy

        self.entry = tk.Entry(self, textvariable=self.var, width=6, justify="center",
                              bg=DARK["entry_bg"], fg=DARK["text"],
                              insertbackground=DARK["text"], relief="flat",
                              font=("Consolas", 12), bd=4)
        self.entry.pack(side="left")

        arrows = tk.Frame(self, bg=DARK["bg"])
        arrows.pack(side="left", padx=(2, 0), fill="y")
        for text, step in (("▲", 1), ("▼", -1)):
            tk.Button(arrows, text=text, command=lambda s=step: self.step(s),
                      font=("Segoe UI", 6), width=2, pady=0, bd=0, relief="flat",
                      bg=DARK["surface2"], fg=DARK["text"],
                      activebackground=DARK["accent2"], activeforeground="#fff",
                      repeatdelay=400, repeatinterval=80, cursor="hand2",
                      takefocus=False).pack(fill="both", expand=True,
                                            pady=(0, 1) if step == 1 else 0)

        self.entry.bind("<KeyPress>", self._on_key)
        self.entry.bind("<<Paste>>", self._on_paste)
        for ev in ("<<Cut>>", "<<Clear>>", "<<PasteSelection>>"):
            self.entry.bind(ev, lambda e: "break")
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<MouseWheel>", lambda e: self.step(1 if e.delta > 0 else -1))
        self.entry.bind("<Button-4>", lambda e: self.step(1))
        self.entry.bind("<Button-5>", lambda e: self.step(-1))

        self.set_seconds(seconds)

    # ── Value ─────────────────────────────────────────────

    def set_seconds(self, seconds: float):
        self._exact = float(seconds)
        self._digits = seconds_to_time_digits(seconds)
        self._render()

    def get_seconds(self) -> float:
        self.normalize()
        if self._exact is not None:
            return self._exact
        return float(time_digits_to_seconds(self._digits))

    def is_valid(self) -> bool:
        return int(mask_time_digits(self._digits)[3:]) < 60

    def normalize(self):
        if not self.is_valid():
            self._set_digits(seconds_to_time_digits(time_digits_to_seconds(self._digits)))

    def step(self, delta: int):
        current = self._exact if self._exact is not None else time_digits_to_seconds(self._digits)
        total = min(max(int(current) + delta, 0), TIME_MASK_MAX_SECONDS)
        self._fresh = False
        self._set_digits(seconds_to_time_digits(total))
        return "break"

    def _set_digits(self, digits: str):
        self._digits = digits.lstrip("0")
        self._exact = None
        self._render()

    def _render(self):
        self.var.set(mask_time_digits(self._digits))
        self.entry.config(fg=DARK["text"] if self.is_valid() else DARK["accent"])
        self.entry.icursor("end")

    # ── Events ────────────────────────────────────────────

    def _on_key(self, e):
        if e.keysym in ("Up", "Down"):
            return self.step(1 if e.keysym == "Up" else -1)
        if e.keysym in self._PASS_KEYS:
            return None
        if e.state & 0x4:  # Control: let shortcuts (Ctrl+S…) through, then undo any edit
            self._later(self._render)
            return None
        if e.char and e.char in "0123456789":
            digits = "" if self._fresh else self._digits
            self._fresh = False
            if len(digits) < TIME_MASK_DIGITS:
                self._set_digits(digits + e.char)
            else:
                self._render()
        elif e.keysym == "BackSpace":
            self._fresh = False
            self._set_digits(self._digits[:-1])
        elif e.keysym == "Delete":
            self._fresh = False
            self._set_digits("")
        return "break"

    def _on_paste(self, _e=None):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        digits = "".join(c for c in text if c in "0123456789")
        if digits:
            self._fresh = False
            self._set_digits(digits.lstrip("0")[-TIME_MASK_DIGITS:])
        return "break"

    def _on_focus_in(self, _e=None):
        self._fresh = True
        self._later(lambda: self.entry.select_range(0, "end"))

    def _later(self, fn):
        """after_idle that is cancelled if the field is destroyed first."""
        self._pending.add(self.after_idle(fn))

    def destroy(self):
        for job in self._pending:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._pending.clear()
        super().destroy()

    def _on_focus_out(self, _e=None):
        self._fresh = False
        self.entry.selection_clear()
        self.normalize()


class StropheEditor(tk.Toplevel):
    """Dialog to add/edit a strophe."""
    def __init__(self, parent, strophe: Optional[Strophe] = None, on_save=None,
                 default_start: float = 0.0, default_end: float = 10.0):
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
        else:
            self.start_entry.set_seconds(default_start)
            self.end_entry.set_seconds(default_end)

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
        self.start_entry = TimeEntry(time_frame, 0)
        self.start_entry.grid(row=1, column=0, padx=(0, 16), sticky="w")

        tk.Label(time_frame, text="Fim (MM:SS)", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9)).grid(row=0, column=1, sticky="w")
        self.end_entry = TimeEntry(time_frame, 10)
        self.end_entry.grid(row=1, column=1, sticky="w")

        # Tip
        tk.Label(self, text="Digite só os números (ex.: 130 = 01:30)  |  ▲▼, setas ou roda do mouse: ±1s",
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
        self.start_entry.set_seconds(s.start_time)
        self.end_entry.set_seconds(s.end_time)
        self.text_widget.delete("1.0", "end")
        self.text_widget.insert("1.0", s.text)

    def _save(self):
        start = self.start_entry.get_seconds()
        end = self.end_entry.get_seconds()

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

    def _spin(self, parent, var, from_, to, width=7, increment=1):
        s = tk.Spinbox(parent, textvariable=var, from_=from_, to=to, width=width,
                       increment=increment,
                       bg=DARK["entry_bg"], fg=DARK["text"], buttonbackground=DARK["surface2"],
                       insertbackground=DARK["text"], relief="flat", font=("Segoe UI", 10),
                       command=self._notify)
        s.bind("<FocusOut>", lambda _: self._notify())
        return s

    def _color_btn(self, parent, var):
        from tkinter import colorchooser
        btn = tk.Button(parent, bg=var.get(), width=4, relief="flat", bd=0,
                        cursor="hand2")
        # Keep the swatch in sync when the var changes (e.g. project opened)
        var.trace_add("write", lambda *_: btn.config(bg=var.get()))
        def pick():
            c = colorchooser.askcolor(color=var.get(), parent=self)[1]
            if c:
                var.set(c)
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
        bind_wheel_scroll(outer)
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
        self._bg_image_row = bg_row
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
        self._row(inner, "Fade (seg)", lambda f: self._spin(f, self.fade_var, 0.1, 5.0, 7, 0.1))

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
        self.res_var.set(f"{p.video_width}x{p.video_height}")
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
            self._bg_color_row.pack(fill="x", padx=12, pady=3, before=self._bg_image_row)

    def load(self, project: Project):
        """Point the panel at another project and show its values."""
        self.project = project
        p = project
        self.title_var.set(p.title)
        self.audio_var.set(p.audio_file)
        self.title_font_var.set(p.title_font)
        self.title_size_var.set(p.title_size)
        self.lyric_font_var.set(p.lyric_font)
        self.lyric_size_var.set(p.lyric_size)
        self.line_spacing_var.set(p.line_spacing)
        self.title_color_var.set(p.title_color)
        self.text_color_var.set(p.text_color)
        self.transparent_var.set(p.transparent_bg)
        self.bg_color_var.set(p.bg_color)
        self.bg_image_var.set(p.bg_image)
        self.fade_var.set(p.fade_duration)
        self.fps_var.set(p.fps)
        self.res_var.set(f"{p.video_width}x{p.video_height}")
        self._on_transparent_toggle()

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

    def _apply(self) -> bool:
        """Copy the form into the project. Returns False if a field is invalid."""
        try:
            title_size   = int(self.title_size_var.get())
            lyric_size   = int(self.lyric_size_var.get())
            line_spacing = int(self.line_spacing_var.get())
            fade         = float(self.fade_var.get())
            fps          = int(self.fps_var.get())
            if title_size <= 0 or lyric_size <= 0 or fps <= 0 or fade < 0 or line_spacing < 0:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showerror("Configuração inválida",
                                 "Confira os campos numéricos (tamanhos, espaçamento, "
                                 "fade e FPS).", parent=self)
            return False

        p = self.project
        p.title        = self.title_var.get().strip() or "Título"
        p.title_font   = self.title_font_var.get().strip() or "mvboli"
        p.title_size   = title_size
        p.lyric_font   = self.lyric_font_var.get().strip() or "mvboli"
        p.lyric_size   = lyric_size
        p.line_spacing = line_spacing
        p.fade_duration = fade
        p.fps          = fps
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
        return True

    def _notify(self):
        pass  # apply only on button click


class StropheList(tk.Frame):
    def __init__(self, parent, project: Project, on_change=None, on_auto_lyrics=None):
        super().__init__(parent, bg=DARK["bg"])
        self.project = project
        self.on_change = on_change
        self.on_auto_lyrics = on_auto_lyrics
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
        if self.on_auto_lyrics:
            StyledButton(hdr, "🎤 Gerar Legenda", command=self.on_auto_lyrics,
                         style="success", padx=10, pady=5).pack(side="right", padx=(0, 6))

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
        bind_wheel_scroll(self.canvas)

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
            self._strophe_card(self.scrollable_frame, s, i, is_last=i == len(strophes) - 1)

    def _strophe_card(self, parent, s: Strophe, idx: int, is_last: bool = False):
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

        n_lines = len([l for l in s.text.split("\n") if l.strip()])
        time_str = (f"⏱  {format_time(s.start_time)}  →  {format_time(s.end_time)}"
                    f"   ({s.end_time - s.start_time:.1f}s · "
                    f"{n_lines} linha{'s' if n_lines != 1 else ''})")
        tk.Label(top, text=time_str, bg=DARK["surface"], fg=DARK["accent"],
                 font=("Courier New", 10, "bold")).pack(side="left")

        StyledButton(top, "✎", command=lambda sid=s.id: self._edit(sid),
                     style="secondary", padx=6, pady=2,
                     font=("Segoe UI", 9)).pack(side="right", padx=(4, 0))
        StyledButton(top, "✕", command=lambda sid=s.id: self._delete(sid),
                     style="danger", padx=6, pady=2,
                     font=("Segoe UI", 9)).pack(side="right")
        if not is_last:
            StyledButton(top, "↓ Juntar", command=lambda sid=s.id: self._merge_next(sid),
                         style="secondary", padx=6, pady=2,
                         font=("Segoe UI", 9)).pack(side="right", padx=(0, 4))

        # Lyrics preview
        preview = s.text.strip()[:120] + ("…" if len(s.text.strip()) > 120 else "")
        tk.Label(inner, text=preview, bg=DARK["surface"], fg=DARK["text"],
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=600).pack(fill="x", pady=(4, 0))

    def _add_strophe(self):
        next_start, next_end = next_strophe_times(self.project.strophes)

        def on_save(s: Strophe):
            s.id = max((x.id for x in self.project.strophes), default=0) + 1
            self.project.strophes.append(s)
            self.refresh()
            if self.on_change:
                self.on_change()

        StropheEditor(self, on_save=on_save,
                      default_start=next_start, default_end=next_end)

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

    def _merge_next(self, sid: int):
        """Join this strophe with the one below it."""
        self.project.strophes = merge_with_next(self.project.strophes, sid)
        self.refresh()
        if self.on_change:
            self.on_change()

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

        next_id = max((s.id for s in self.project.strophes), default=0) + 1
        new_strophes, errors = parse_lyrics_block(raw, next_id)
        self.project.strophes.extend(new_strophes)
        imported = len(new_strophes)

        if errors:
            msg = f"Importadas: {imported}\n\nErros ({len(errors)}):\n" + "\n".join(errors[:5])
            messagebox.showwarning("Importação parcial", msg, parent=self)
            if not imported:
                return  # keep the dialog (and the pasted text) open to fix it
        else:
            messagebox.showinfo("Sucesso", f"{imported} estrofe(s) importada(s)!", parent=self)

        if self.on_done:
            self.on_done()
        self.destroy()


class AutoLyricsDialog(tk.Toplevel):
    """
    Generate timed strophes from the audio with Whisper (auto_lyrics.py).
    With lyrics pasted, the text is kept and only the timing is detected;
    without, Whisper also writes the text.
    """
    LANGUAGES = {"Português": "pt", "Inglês": "en", "Espanhol": "es", "Detectar": None}

    def __init__(self, parent, project: Project, on_audio_change=None, on_done=None):
        super().__init__(parent)
        self.project = project
        self.on_audio_change = on_audio_change
        self.on_done = on_done
        self.cancelled = False
        self.running = False
        self.title("Gerar Legenda Automática")
        self.configure(bg=DARK["bg"])
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._build()
        self.geometry("640x620")
        self.transient(parent)

    def _build(self):
        pad = {"padx": 16}
        tk.Label(self, text="🎤  Gerar Legenda Automática", bg=DARK["bg"], fg=DARK["text"],
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(14, 8), **pad)

        # Audio
        row = tk.Frame(self, bg=DARK["bg"])
        row.pack(fill="x", **pad)
        tk.Label(row, text="Áudio:", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=8, anchor="w").pack(side="left")
        self.audio_var = tk.StringVar(value=self.project.audio_file)
        tk.Entry(row, textvariable=self.audio_var, bg=DARK["entry_bg"], fg=DARK["text"],
                 insertbackground=DARK["text"], relief="flat", font=("Segoe UI", 9),
                 bd=4).pack(side="left", fill="x", expand=True, padx=(0, 6))
        StyledButton(row, "...", command=self._pick_audio, style="secondary",
                     padx=8, pady=4).pack(side="left")

        # Options
        opt = tk.Frame(self, bg=DARK["bg"])
        opt.pack(fill="x", pady=(8, 0), **pad)
        tk.Label(opt, text="Idioma:", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9), width=8, anchor="w").pack(side="left")
        self.lang_var = tk.StringVar(value="Português")
        ttk.Combobox(opt, textvariable=self.lang_var, values=list(self.LANGUAGES),
                     state="readonly", width=12).pack(side="left")
        tk.Label(opt, text="Modelo:", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(16, 6))
        self.model_var = tk.StringVar(value=auto_lyrics.MODELS[auto_lyrics.DEFAULT_MODEL])
        model_box = ttk.Combobox(opt, textvariable=self.model_var,
                                 values=list(auto_lyrics.MODELS.values()),
                                 state="readonly", width=16)
        model_box.pack(side="left")
        model_box.bind("<<ComboboxSelected>>", lambda _: self._show_model_status())

        # Lyrics
        tk.Label(self, text="Letra (opcional, mas deixa bem mais preciso):", bg=DARK["bg"],
                 fg=DARK["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w", pady=(14, 2), **pad)
        tk.Label(self, text="Cole a letra com as estrofes separadas por linha em branco — "
                            "o texto fica igualzinho e só os tempos são detectados.\n"
                            "Sem letra, a IA escreve sozinha (pode errar algumas palavras).",
                 bg=DARK["bg"], fg=DARK["text_dim"], font=("Segoe UI", 8),
                 justify="left").pack(anchor="w", **pad)
        tf = tk.Frame(self, bg=DARK["border"], padx=1, pady=1)
        tf.pack(fill="both", expand=True, pady=(6, 0), **pad)
        self.lyrics_text = tk.Text(tf, bg=DARK["entry_bg"], fg=DARK["text"],
                                   insertbackground=DARK["text"], font=("Segoe UI", 10),
                                   relief="flat", bd=8, wrap="word", height=10)
        self.lyrics_text.pack(fill="both", expand=True)

        # Progress
        self.status_label = tk.Label(self, text="", bg=DARK["bg"], fg=DARK["text_dim"],
                                     font=("Segoe UI", 9))
        self._show_model_status()
        self.status_label.pack(anchor="w", pady=(10, 2), **pad)
        self.progress_bar = ttk.Progressbar(self, mode="determinate")
        self.progress_bar.pack(fill="x", **pad)

        # Buttons
        bf = tk.Frame(self, bg=DARK["bg"])
        bf.pack(fill="x", pady=12, **pad)
        self.cancel_btn = StyledButton(bf, "✕  Cancelar", command=self._cancel, style="secondary")
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.gen_btn = StyledButton(bf, "🎤  Gerar", command=self._start)
        self.gen_btn.pack(side="right")

    def _pick_audio(self):
        path = filedialog.askopenfilename(
            parent=self,
            filetypes=[("Áudio", "*.mp3 *.wav *.ogg *.aac *.m4a *.flac"), ("Todos", "*.*")])
        if path:
            self.audio_var.set(path)

    def _show_model_status(self):
        model = self._model_size()
        if auto_lyrics.is_downloaded(model):
            text = "✓ Modelo já baixado — funciona sem internet."
        else:
            text = (f"Este modelo ainda não foi baixado: na primeira vez ele baixa "
                    f"{auto_lyrics.MODEL_DOWNLOAD_SIZE[model]} da internet.")
        self.status_label.config(text=text, fg=DARK["text_dim"])

    def _model_size(self) -> str:
        label = self.model_var.get()
        return next((k for k, v in auto_lyrics.MODELS.items() if v == label),
                    auto_lyrics.DEFAULT_MODEL)

    def _cancel(self):
        self.cancelled = True
        self.running = False
        self.destroy()

    def _start(self):
        audio = self.audio_var.get().strip()
        if not audio or not os.path.isfile(audio):
            messagebox.showerror("Erro", "Escolha o arquivo de áudio da música.", parent=self)
            return
        if not auto_lyrics.is_available():
            messagebox.showerror("Whisper não instalado",
                                 "Instale o Whisper com:\n\n    pip install faster-whisper\n\n"
                                 "e abra o programa de novo.", parent=self)
            return
        if audio != self.project.audio_file and self.on_audio_change:
            self.on_audio_change(audio)  # also use it for the render

        lyrics = self.lyrics_text.get("1.0", "end-1c")
        model = self._model_size()
        language = self.LANGUAGES.get(self.lang_var.get(), "pt")

        self.gen_btn.config(state="disabled")
        self.cancel_btn.config(text="⏹  Parar")
        if auto_lyrics.is_downloaded(model):
            self.status_label.config(text="Carregando o modelo…")
        else:
            self.status_label.config(text=f"Baixando o modelo "
                                          f"({auto_lyrics.MODEL_DOWNLOAD_SIZE[model]}, só desta vez)…")
        self.progress_bar.config(mode="indeterminate")
        self.progress_bar.start(12)
        self.running = True

        # The worker never touches Tk: it queues callbacks that _poll runs
        # on the main thread.
        events = queue.Queue()
        post = events.put

        def poll():
            try:
                while True:
                    events.get_nowait()()
            except queue.Empty:
                pass
            if self.running:
                self.after(100, poll)

        self.after(100, poll)

        def progress(p):
            post(lambda v=int(p * 100): self._set_progress(v))

        def run():
            try:
                result = auto_lyrics.generate(audio, lyrics, model, language,
                                              progress=progress,
                                              cancel=lambda: self.cancelled)
                outcome = (True, result)
            except auto_lyrics.Cancelled:
                return
            except auto_lyrics.AutoLyricsError as e:
                outcome = (False, str(e))
            except Exception as e:
                outcome = (False, f"Erro inesperado: {e}")
            if not self.cancelled:
                post(lambda: self._on_done(*outcome))

        threading.Thread(target=run, daemon=True).start()

    def _set_progress(self, pct: int):
        try:
            if str(self.progress_bar.cget("mode")) != "determinate":
                self.progress_bar.stop()
                self.progress_bar.config(mode="determinate")
            self.progress_bar["value"] = pct
            self.status_label.config(text=f"Ouvindo a música… {pct}%")
        except tk.TclError:
            pass

    def _on_done(self, ok: bool, result):
        self.running = False
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate")
        if not ok:
            self.status_label.config(text="❌  Erro ao gerar a legenda", fg=DARK["accent"])
            self.gen_btn.config(state="normal")
            self.cancel_btn.config(text="✕  Cancelar")
            messagebox.showerror("Erro", result, parent=self)
            return
        if not result:
            self.gen_btn.config(state="normal")
            messagebox.showwarning("Nada encontrado", "Nenhuma estrofe foi gerada.", parent=self)
            return
        if self.project.strophes and not messagebox.askyesno(
                "Substituir estrofes?",
                f"O projeto já tem {len(self.project.strophes)} estrofe(s).\n"
                f"Substituir pelas {len(result)} geradas?", parent=self):
            self.gen_btn.config(state="normal")
            self.cancel_btn.config(text="✕  Cancelar")
            return

        self.project.strophes = [Strophe(id=i + 1, start_time=s.start, end_time=s.end, text=s.text)
                                 for i, s in enumerate(result)]
        if self.on_done:
            self.on_done()
        messagebox.showinfo("Pronto",
                            f"{len(result)} estrofe(s) gerada(s)!\n\n"
                            "Revise os tempos e o texto antes de renderizar.", parent=self)
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
        # Closing the window must stop the render too
        self.protocol("WM_DELETE_WINDOW", self._cancel)
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
            os.path.expanduser("~"), f"{safe_filename(self.project.title)}.mp4"
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
            initialfile=f"{safe_filename(self.project.title)}.mp4"
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

        try:
            renderer = FrameRenderer(self.project)
        except Exception as e:
            self.render_btn.config(state="normal")
            messagebox.showerror("Erro", f"Não foi possível preparar o render:\n{e}", parent=self)
            return

        # IMPORTANT: never touch tkinter widgets from background thread on Windows.
        # Use after() to post updates back to the main thread.
        def post(fn):
            try:
                self.after(0, fn)
            except (tk.TclError, RuntimeError):
                pass  # dialog was closed while rendering

        def progress(p):
            pct = int(p * 100)
            post(lambda v=pct: self._set_progress(v))

        def run():
            try:
                ok, msg = renderer.render_video(
                    out,
                    audio_path=self.project.audio_file,
                    progress_callback=progress,
                    cancel_flag=lambda: self.cancelled,
                )
            except Exception as e:
                ok, msg = False, f"Erro inesperado: {e}"
            if not self.cancelled:
                post(lambda: self._on_done(ok, msg))

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
                                         on_change=self._on_project_change,
                                         on_auto_lyrics=self._auto_lyrics)
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
            self._load_project(Project(), None)

    def _load_project(self, project: Project, path: Optional[str]):
        """Swap the current project and refresh both panels."""
        self.project = project
        self._current_file = path
        self.settings.load(project)
        self.strophe_list.project = project
        self.strophe_list.refresh()
        self._on_project_change()

    def _open_project(self):
        path = filedialog.askopenfilename(filetypes=[("LyricRenderer", "*.lyr"),
                                                     ("JSON", "*.json"), ("Todos", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._load_project(Project.from_dict(data), path)
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
            initialfile=f"{safe_filename(self.project.title)}.lyr"
        )
        if path:
            self._current_file = path
            self._do_save(path)

    def _do_save(self, path: str):
        if not self.settings._apply():
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.project.to_dict(), f, ensure_ascii=False, indent=2)
            self._on_project_change()
            self.statusbar.config(text=f"Salvo em {path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível salvar:\n{e}")

    def _auto_lyrics(self):
        if not self.settings._apply():
            return

        def on_audio_change(path):
            self.settings.audio_var.set(path)
            self.project.audio_file = path

        def on_done():
            self.strophe_list.refresh()
            self._on_project_change()

        AutoLyricsDialog(self, self.project, on_audio_change=on_audio_change, on_done=on_done)

    def _render(self):
        if not self.settings._apply():
            return
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
