#!/usr/bin/env python3
"""
LyricRenderer — Gerador de vídeos de letras de música
Renderiza estrofes com fade in/out transparente sobre fundo preto/personalizado.
"""
from __future__ import annotations  # PIL types in hints even when Pillow is missing

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
import sys
import subprocess
import threading
import queue
import re
import functools
import traceback
from pathlib import Path
from dataclasses import dataclass, field, asdict, replace
from typing import List, Optional
import colorsys

import auto_lyrics
import deps

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    HAS_PIL = True
    PIL_ERROR = ""
except ImportError as _e:  # missing, or its DLLs were blocked
    HAS_PIL = False
    PIL_ERROR = str(_e)


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


def title_from_filename(path: str) -> str:
    """
    Song title from the audio file name: underscores/hyphens become spaces,
    track numbers and copy markers like "(1)" are dropped, and every word gets
    a capital first letter. "03_onde_eu_fui (1).mp3" → "Onde Eu Fui".
    """
    name = os.path.splitext(os.path.basename(path))[0]
    name = name.replace("_", " ")
    if " " not in name.strip():
        name = name.replace("-", " ")  # slug style: onde-eu-fui
    name = re.sub(r"\s*[\(\[]\s*\d+\s*[\)\]]\s*$", "", name)  # "(1)", "[2]"
    words = name.split()
    # leading track number: zero-padded ("03") or followed by - . ) — but
    # keep numbers that are part of the name ("22 de Outubro")
    while len(words) > 1 and (re.fullmatch(r"0\d*[.)-]?|\d+[.)-]", words[0])
                              or (words[0].isdigit() and words[1] == "-")):
        words.pop(0)
    while len(words) > 1 and words[-1].isdigit():
        words.pop()  # trailing copy number
    words = [w for w in words if w != "-"]
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


# ─────────────────────────────────────────────
#  App config (settings that rarely change, kept between sessions)
# ─────────────────────────────────────────────

STYLE_FIELDS = ("title_font", "title_size", "lyric_font", "lyric_size", "line_spacing",
                "fade_duration", "text_color", "title_color", "transparent_bg", "bg_color",
                "video_width", "video_height", "fps")


def config_path() -> str:
    """Where the app config lives (LYRIC_RENDERER_CONFIG overrides it)."""
    override = os.environ.get("LYRIC_RENDERER_CONFIG")
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "LyricRenderer", "config.json")


def load_config(path: Optional[str] = None) -> dict:
    try:
        with open(path or config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict, path: Optional[str] = None):
    path = path or config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # losing preferences isn't worth interrupting the user


def style_of(project: "Project") -> dict:
    return {f: getattr(project, f) for f in STYLE_FIELDS}


def apply_style(project: "Project", cfg: dict):
    """Copy saved style settings into the project, skipping wrong types."""
    defaults = Project()
    for f in STYLE_FIELDS:
        if f not in cfg:
            continue
        value, default = cfg[f], getattr(defaults, f)
        if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if type(value) is type(default):
            setattr(project, f, value)


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

    path = _search_font_file(font_name)
    if path:
        try:
            return ImageFont.truetype(path, size)
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


@functools.lru_cache(maxsize=32)
def _search_font_file(font_name: str) -> Optional[str]:
    """Walk the system font folders once per name (the preview asks a lot)."""
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
                    path = os.path.join(root, f)
                    try:
                        ImageFont.truetype(path, 12)  # skip broken files
                        return path
                    except Exception:
                        pass
    return None


@functools.lru_cache(maxsize=4)
def _load_background(path: str, mtime: float, w: int, h: int):
    """Background image resized to the video size (cached for the preview)."""
    return Image.open(path).convert("RGB").resize((w, h), Image.LANCZOS)


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


def final_output_path(output_path: str, transparent: bool) -> str:
    """The file actually written: transparent video is always .webm."""
    if transparent and Path(output_path).suffix.lower() != ".webm":
        return Path(output_path).with_suffix(".webm").as_posix()
    return output_path


def project_is_transparent(project: Project) -> bool:
    """Same rule as the renderer: a background image disables transparency."""
    return project.transparent_bg and not (project.bg_image and os.path.isfile(project.bg_image))


def build_ffmpeg_cmd(output_path: str, width: int, height: int, fps: int,
                     transparent: bool, audio_path: str = ""):
    """
    Build the ffmpeg command for a raw-frame pipe on stdin.
    Returns (cmd, final_output_path).
    - Transparent → always WebM (VP9 + alpha)
    - .webm       → VP9 + Opus (WebM does not accept H.264/AAC)
    - otherwise   → H.264 + AAC
    """
    output_path = final_output_path(output_path, transparent)
    webm = Path(output_path).suffix.lower() == ".webm"

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
                self._bg_img = _load_background(project.bg_image,
                                                os.path.getmtime(project.bg_image),
                                                self.w, self.h)
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
    "bg": "#101017",
    "surface": "#181822",
    "surface2": "#262634",
    "accent": "#e94560",
    "accent2": "#6f4fc8",
    "text": "#ececf3",
    "text_dim": "#8b8ba6",
    "border": "#2c2c3d",
    "success": "#3fb67a",
    "warn": "#ff9800",
    "entry_bg": "#0c0c12",
    "selected": "#e94560",
}
UI_FONT = "Segoe UI"


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
    STYLES = {
        "primary": (DARK["accent"], "#fff"),
        "secondary": (DARK["surface2"], DARK["text"]),
        "ghost": (DARK["surface"], DARK["text"]),
        "danger": ("#5a1f2c", "#ff9a9a"),
        "success": ("#1d5a3c", "#b5f0cf"),
    }

    def __init__(self, parent, text, command=None, style="primary", **kwargs):
        bg, fg = self.STYLES.get(style, self.STYLES["primary"])
        # Use caller-supplied padx/pady if provided, else defaults
        kwargs.setdefault("padx", 14)
        kwargs.setdefault("pady", 7)
        kwargs.setdefault("font", (UI_FONT, 10, "bold"))
        super().__init__(
            parent, text=text, command=command,
            bg=bg, fg=fg, activebackground=self._lighten(bg),
            activeforeground=fg, relief="flat",
            bd=0, cursor="hand2", disabledforeground=DARK["text_dim"],
            highlightthickness=0,
            **kwargs
        )
        self.bind("<Enter>", lambda e: self.config(bg=self._lighten(bg)))
        self.bind("<Leave>", lambda e: self.config(bg=bg))

    @staticmethod
    def _lighten(hex_color):
        try:
            r, g, b = hex_to_rgb(hex_color)
            h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
            v = min(1.0, v + 0.12)
            r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
            return f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"
        except Exception:
            return hex_color


def styled_entry(parent, var, font_size=10, **kw):
    """Flat dark entry with an accent focus ring."""
    return tk.Entry(parent, textvariable=var, bg=DARK["entry_bg"], fg=DARK["text"],
                    insertbackground=DARK["text"], relief="flat", bd=6,
                    font=(UI_FONT, font_size), highlightthickness=1,
                    highlightbackground=DARK["border"], highlightcolor=DARK["accent"], **kw)


def section_title(parent, text, bg):
    return tk.Label(parent, text=text.upper(), bg=bg, fg=DARK["text_dim"],
                    font=(UI_FONT, 8, "bold"))


def open_folder(folder: str):
    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.run(["open", folder])
    else:
        subprocess.run(["xdg-open", folder])


class WorkerMixin:
    """
    Run a function in a background thread; it reports back by queueing
    callbacks that are executed on the Tk thread (Tk is not thread-safe).
    """

    def start_worker(self, target):
        self._events = queue.Queue()
        self._working = True

        def poll():
            try:
                while True:
                    self._events.get_nowait()()
            except queue.Empty:
                pass
            if self._working:
                self.after(100, poll)

        self.after(100, poll)
        threading.Thread(target=target, daemon=True).start()

    def post(self, fn):
        """Thread-safe: run fn on the Tk thread."""
        self._events.put(fn)

    def stop_worker(self):
        self._working = False


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
    """Look-and-video settings (fonts, colors, background color, fps…).
    Nothing is written to the project until _apply()."""

    def __init__(self, parent, project: Project, on_change=None):
        super().__init__(parent, bg=DARK["surface"])
        self.project = project
        self.on_change = on_change
        self._build()

    def _section(self, text):
        f = tk.Frame(self._inner, bg=DARK["surface"])
        f.pack(fill="x", padx=16, pady=(16, 6))
        section_title(f, text, DARK["surface"]).pack(anchor="w")
        tk.Frame(f, bg=DARK["border"], height=1).pack(fill="x", pady=(4, 0))
        return f

    def _row(self, parent, label, widget_factory, stretch=True):
        row = tk.Frame(parent, bg=DARK["surface"])
        row.pack(fill="x", padx=16, pady=4)
        tk.Label(row, text=label, bg=DARK["surface"], fg=DARK["text_dim"],
                 font=(UI_FONT, 9), width=16, anchor="w").pack(side="left")
        w = widget_factory(row)
        w.pack(side="left", fill="x" if stretch else None, expand=stretch)
        return w

    def _spin(self, parent, var, from_, to, width=7, increment=1):
        return tk.Spinbox(parent, textvariable=var, from_=from_, to=to, width=width,
                          increment=increment,
                          bg=DARK["entry_bg"], fg=DARK["text"], buttonbackground=DARK["surface2"],
                          insertbackground=DARK["text"], relief="flat", font=(UI_FONT, 10),
                          highlightthickness=1, highlightbackground=DARK["border"],
                          highlightcolor=DARK["accent"])

    def _color_btn(self, parent, var):
        from tkinter import colorchooser
        btn = tk.Button(parent, bg=var.get(), width=6, relief="flat", bd=0,
                        cursor="hand2", highlightthickness=1,
                        highlightbackground=DARK["border"])
        # Keep the swatch in sync when the var changes (e.g. project opened)
        var.trace_add("write", lambda *_: btn.config(bg=var.get(), activebackground=var.get()))

        def pick():
            c = colorchooser.askcolor(color=var.get(), parent=self)[1]
            if c:
                var.set(c)
        btn.config(command=pick)
        return btn

    def _build(self):
        p = self.project

        # Scrollable, so it never clips on small screens
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
        self._inner = inner

        # ── Typography ─────────────────────────────────────
        self._section("Texto")
        self.title_font_var = tk.StringVar(value=p.title_font)
        self._row(inner, "Fonte do título", lambda f: styled_entry(f, self.title_font_var))
        self.title_size_var = tk.IntVar(value=p.title_size)
        self._row(inner, "Tamanho do título", lambda f: self._spin(f, self.title_size_var, 20, 200))
        self.lyric_font_var = tk.StringVar(value=p.lyric_font)
        self._row(inner, "Fonte da letra", lambda f: styled_entry(f, self.lyric_font_var))
        self.lyric_size_var = tk.IntVar(value=p.lyric_size)
        self._row(inner, "Tamanho da letra", lambda f: self._spin(f, self.lyric_size_var, 16, 160))
        self.line_spacing_var = tk.IntVar(value=p.line_spacing)
        self._row(inner, "Espaço entre linhas", lambda f: self._spin(f, self.line_spacing_var, 0, 80))

        # ── Colors ─────────────────────────────────────────
        self._section("Cores")
        self.title_color_var = tk.StringVar(value=p.title_color)
        self._row(inner, "Cor do título", lambda f: self._color_btn(f, self.title_color_var), stretch=False)
        self.text_color_var = tk.StringVar(value=p.text_color)
        self._row(inner, "Cor da letra", lambda f: self._color_btn(f, self.text_color_var), stretch=False)

        # ── Background (when there's no image) ─────────────
        self._section("Fundo sem imagem")
        self._transparent_row = tk.Frame(inner, bg=DARK["surface"])
        self._transparent_row.pack(fill="x", padx=16, pady=4)
        self.transparent_var = tk.BooleanVar(value=p.transparent_bg)
        tk.Checkbutton(self._transparent_row, text="Fundo transparente (.webm, pra sobrepor no editor)",
                       variable=self.transparent_var, command=self._on_transparent_toggle,
                       bg=DARK["surface"], fg=DARK["text"], activebackground=DARK["surface"],
                       activeforeground=DARK["text"], selectcolor=DARK["entry_bg"],
                       font=(UI_FONT, 9)).pack(side="left")

        self._bg_color_row = tk.Frame(inner, bg=DARK["surface"])
        self._bg_color_row.pack(fill="x", padx=16, pady=4)
        tk.Label(self._bg_color_row, text="Cor de fundo", bg=DARK["surface"], fg=DARK["text_dim"],
                 font=(UI_FONT, 9), width=16, anchor="w").pack(side="left")
        self.bg_color_var = tk.StringVar(value=p.bg_color)
        self._color_btn(self._bg_color_row, self.bg_color_var).pack(side="left")
        tk.Label(inner, text="Quando há imagem de fundo (tela principal), ela é usada no lugar disso.",
                 bg=DARK["surface"], fg=DARK["text_dim"], font=(UI_FONT, 8)).pack(anchor="w", padx=16)

        # ── Video ──────────────────────────────────────────
        self._section("Vídeo")
        self.fade_var = tk.DoubleVar(value=p.fade_duration)
        self._row(inner, "Fade (segundos)", lambda f: self._spin(f, self.fade_var, 0.1, 5.0, 7, 0.1))
        self.fps_var = tk.IntVar(value=p.fps)
        self._row(inner, "FPS", lambda f: self._spin(f, self.fps_var, 10, 60))
        self.res_var = tk.StringVar(value=f"{p.video_width}x{p.video_height}")
        self._row(inner, "Resolução", lambda f: ttk.Combobox(
            f, textvariable=self.res_var, values=["1920x1080", "1280x720", "3840x2160"],
            state="readonly", width=12, font=(UI_FONT, 10)))

        tk.Frame(inner, bg=DARK["surface"], height=12).pack()
        self._on_transparent_toggle()  # set initial visibility

    def _on_transparent_toggle(self):
        """Show the background color only when the video isn't transparent."""
        if self.transparent_var.get():
            self._bg_color_row.pack_forget()
        else:
            self._bg_color_row.pack(fill="x", padx=16, pady=4, after=self._transparent_row)

    def load(self, project: Project):
        """Point the panel at another project and show its values."""
        self.project = p = project
        self.title_font_var.set(p.title_font)
        self.title_size_var.set(p.title_size)
        self.lyric_font_var.set(p.lyric_font)
        self.lyric_size_var.set(p.lyric_size)
        self.line_spacing_var.set(p.line_spacing)
        self.title_color_var.set(p.title_color)
        self.text_color_var.set(p.text_color)
        self.transparent_var.set(p.transparent_bg)
        self.bg_color_var.set(p.bg_color)
        self.fade_var.set(p.fade_duration)
        self.fps_var.set(p.fps)
        self.res_var.set(f"{p.video_width}x{p.video_height}")
        self._on_transparent_toggle()

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
        p.transparent_bg = self.transparent_var.get()

        res = self.res_var.get()
        if "x" in res:
            w, h = res.split("x")
            p.video_width  = int(w)
            p.video_height = int(h)

        if self.on_change:
            self.on_change()
        return True


class SettingsDialog(tk.Toplevel):
    """⚙ window: settings that rarely change. Saved for next time on 'Salvar'."""

    def __init__(self, parent, project: Project, on_saved=None):
        super().__init__(parent)
        self.on_saved = on_saved
        self.title("Configurações")
        self.configure(bg=DARK["surface"])
        self.geometry("520x640")
        self.minsize(460, 400)
        self.transient(parent)
        self.grab_set()

        bar = tk.Frame(self, bg=DARK["surface"])
        bar.pack(side="bottom", fill="x", padx=16, pady=14)
        StyledButton(bar, "Cancelar", command=self.destroy, style="secondary").pack(side="right")
        StyledButton(bar, "Salvar", command=self._save).pack(side="right", padx=(0, 8))
        tk.Label(bar, text="Fica salvo pras próximas vezes.", bg=DARK["surface"],
                 fg=DARK["text_dim"], font=(UI_FONT, 8)).pack(side="left")

        self.panel = SettingsPanel(self, project)
        self.panel.pack(fill="both", expand=True)

    def _save(self):
        if self.panel._apply():
            if self.on_saved:
                self.on_saved()
            self.destroy()


class StropheList(tk.Frame):
    """Right column: the strophe cards. Clicking a card selects it (preview)."""

    def __init__(self, parent, project: Project, on_change=None, on_auto_lyrics=None,
                 on_select=None):
        super().__init__(parent, bg=DARK["bg"])
        self.project = project
        self.on_change = on_change
        self.on_auto_lyrics = on_auto_lyrics
        self.on_select = on_select
        self.selected_id: Optional[int] = None
        self._cards = {}
        self._build()

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=DARK["bg"])
        hdr.pack(fill="x", padx=(8, 12), pady=(0, 10))
        section_title(hdr, "Legenda", DARK["bg"]).pack(side="left")
        self.count_label = tk.Label(hdr, text="", bg=DARK["bg"], fg=DARK["text_dim"],
                                    font=(UI_FONT, 9))
        self.count_label.pack(side="left", padx=(8, 0))

        StyledButton(hdr, "+ Nova", command=self._add_strophe,
                     style="secondary", padx=12, pady=6).pack(side="right")
        StyledButton(hdr, "📋 Colar", command=self._paste_block,
                     style="secondary", padx=12, pady=6).pack(side="right", padx=(0, 6))
        if self.on_auto_lyrics:
            StyledButton(hdr, "🎤 Gerar automática", command=self.on_auto_lyrics,
                         style="primary", padx=12, pady=6).pack(side="right", padx=(0, 6))

        # Canvas + scrollbar for strophe cards
        container = tk.Frame(self, bg=DARK["bg"])
        container.pack(fill="both", expand=True)

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
        bind_wheel_scroll(self.canvas)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self.refresh()

    def _on_canvas_resize(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def sorted_strophes(self) -> List[Strophe]:
        return sorted(self.project.strophes, key=lambda s: s.start_time)

    def refresh(self):
        for w in self.scrollable_frame.winfo_children():
            w.destroy()
        self._cards = {}

        strophes = self.sorted_strophes()
        n = len(strophes)
        self.count_label.config(text=f"· {n} estrofe{'s' if n != 1 else ''}" if n else "")
        if self.selected_id not in {s.id for s in strophes}:
            self.selected_id = strophes[0].id if strophes else None

        if not strophes:
            tk.Label(self.scrollable_frame,
                     text="Nenhuma estrofe ainda.\n\nEscolha o áudio e clique em "
                          "🎤 Gerar automática,\nou adicione com + Nova / 📋 Colar.",
                     bg=DARK["bg"], fg=DARK["text_dim"],
                     font=(UI_FONT, 11), justify="center").pack(pady=60)
            return

        for i, s in enumerate(strophes):
            self._strophe_card(self.scrollable_frame, s, i, is_last=i == n - 1)

    def select(self, sid: Optional[int]):
        self.selected_id = sid
        for card_sid, card in self._cards.items():
            card.config(highlightbackground=DARK["selected"] if card_sid == sid else DARK["surface"])
        if self.on_select:
            self.on_select(sid)

    def _strophe_card(self, parent, s: Strophe, idx: int, is_last: bool = False):
        selected = s.id == self.selected_id
        card = tk.Frame(parent, bg=DARK["surface"], highlightthickness=2,
                        highlightbackground=DARK["selected"] if selected else DARK["surface"],
                        cursor="hand2")
        card.pack(fill="x", padx=(8, 4), pady=4)
        self._cards[s.id] = card

        # Color accent strip
        strip = tk.Frame(card, bg=DARK["accent"] if idx % 2 == 0 else DARK["accent2"], width=4)
        strip.pack(side="left", fill="y")

        inner = tk.Frame(card, bg=DARK["surface"])
        inner.pack(side="left", fill="both", expand=True, padx=12, pady=10)

        # Top row: time and buttons
        top = tk.Frame(inner, bg=DARK["surface"])
        top.pack(fill="x")

        n_lines = len([l for l in s.text.split("\n") if l.strip()])
        time_str = (f"{format_time(s.start_time)}  →  {format_time(s.end_time)}")
        tk.Label(top, text=time_str, bg=DARK["surface"], fg=DARK["text"],
                 font=("Consolas", 10, "bold")).pack(side="left")
        tk.Label(top, text=f"   {s.end_time - s.start_time:.1f}s · "
                           f"{n_lines} linha{'s' if n_lines != 1 else ''}",
                 bg=DARK["surface"], fg=DARK["text_dim"], font=(UI_FONT, 9)).pack(side="left")

        StyledButton(top, "✎", command=lambda sid=s.id: self._edit(sid),
                     style="secondary", padx=7, pady=1,
                     font=(UI_FONT, 9)).pack(side="right", padx=(4, 0))
        StyledButton(top, "✕", command=lambda sid=s.id: self._delete(sid),
                     style="danger", padx=7, pady=1,
                     font=(UI_FONT, 9)).pack(side="right")
        if not is_last:
            StyledButton(top, "↓ Juntar", command=lambda sid=s.id: self._merge_next(sid),
                         style="secondary", padx=7, pady=1,
                         font=(UI_FONT, 9)).pack(side="right", padx=(0, 4))

        # Lyrics
        tk.Label(inner, text=s.text.strip(), bg=DARK["surface"], fg=DARK["text"],
                 font=(UI_FONT, 10), anchor="w", justify="left",
                 wraplength=560).pack(fill="x", pady=(6, 0))

        # Click anywhere (except the buttons) selects the card
        def bind_select(w):
            if not isinstance(w, tk.Button):
                w.bind("<Button-1>", lambda e, sid=s.id: self.select(sid))
                w.bind("<Double-Button-1>", lambda e, sid=s.id: self._edit(sid))
            for child in w.winfo_children():
                bind_select(child)
        bind_select(card)

    def _changed(self):
        self.refresh()
        if self.on_change:
            self.on_change()

    def _add_strophe(self):
        next_start, next_end = next_strophe_times(self.project.strophes)

        def on_save(s: Strophe):
            s.id = max((x.id for x in self.project.strophes), default=0) + 1
            self.project.strophes.append(s)
            self.selected_id = s.id
            self._changed()

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
            self._changed()

        StropheEditor(self, strophe=s, on_save=on_save)

    def _merge_next(self, sid: int):
        """Join this strophe with the one below it."""
        self.project.strophes = merge_with_next(self.project.strophes, sid)
        self.selected_id = sid
        self._changed()

    def _delete(self, sid: int):
        if messagebox.askyesno("Confirmar", "Remover esta estrofe?"):
            self.project.strophes = [s for s in self.project.strophes if s.id != sid]
            self._changed()

    def _paste_block(self):
        """Quick paste dialog: paste multiple strophes at once."""
        PasteBlockDialog(self, self.project, on_done=self._changed)


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


class AutoLyricsDialog(WorkerMixin, tk.Toplevel):
    """
    Generate timed strophes from the audio with Whisper (auto_lyrics.py).
    With lyrics pasted, the text is kept and only the timing is detected;
    without, Whisper also writes the text.
    """
    LANGUAGES = {"Português": "pt", "Inglês": "en", "Espanhol": "es", "Detectar": None}

    def __init__(self, parent, project: Project, options: Optional[dict] = None,
                 on_options=None, on_done=None, ask_before_replace: bool = True):
        super().__init__(parent)
        self.project = project
        self.options = options or {}
        self.on_options = on_options
        self.on_done = on_done
        self.ask_before_replace = ask_before_replace
        self.cancelled = False
        self.running = False
        self.title("Gerar legenda automática")
        self.configure(bg=DARK["bg"])
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._build()
        self.geometry("640x640")
        self.transient(parent)

    def _build(self):
        pad = {"padx": 20}
        tk.Label(self, text="🎤  Gerar legenda automática", bg=DARK["bg"], fg=DARK["text"],
                 font=(UI_FONT, 13, "bold")).pack(anchor="w", pady=(16, 2), **pad)
        tk.Label(self, text=f"Música: {os.path.basename(self.project.audio_file)}",
                 bg=DARK["bg"], fg=DARK["text_dim"], font=(UI_FONT, 9)).pack(anchor="w", **pad)

        # Options
        opt = tk.Frame(self, bg=DARK["bg"])
        opt.pack(fill="x", pady=(12, 0), **pad)
        tk.Label(opt, text="Idioma", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=(UI_FONT, 9)).pack(side="left", padx=(0, 6))
        lang = self.options.get("language", "Português")
        self.lang_var = tk.StringVar(value=lang if lang in self.LANGUAGES else "Português")
        ttk.Combobox(opt, textvariable=self.lang_var, values=list(self.LANGUAGES),
                     state="readonly", width=12).pack(side="left")
        tk.Label(opt, text="Modelo", bg=DARK["bg"], fg=DARK["text_dim"],
                 font=(UI_FONT, 9)).pack(side="left", padx=(18, 6))
        model = self.options.get("whisper_model", auto_lyrics.DEFAULT_MODEL)
        if model not in auto_lyrics.MODELS:
            model = auto_lyrics.DEFAULT_MODEL
        self.model_var = tk.StringVar(value=auto_lyrics.MODELS[model])
        model_box = ttk.Combobox(opt, textvariable=self.model_var,
                                 values=list(auto_lyrics.MODELS.values()),
                                 state="readonly", width=16)
        model_box.pack(side="left")
        model_box.bind("<<ComboboxSelected>>", lambda _: self._show_model_status())

        # Lyrics
        tk.Label(self, text="Letra (opcional, mas deixa bem mais preciso)", bg=DARK["bg"],
                 fg=DARK["text"], font=(UI_FONT, 10, "bold")).pack(anchor="w", pady=(16, 2), **pad)
        tk.Label(self, text="Cole a letra com as estrofes separadas por linha em branco — o texto "
                            "fica igualzinho e só os tempos são detectados.\n"
                            "Sem letra, a IA escreve sozinha (pode errar algumas palavras).",
                 bg=DARK["bg"], fg=DARK["text_dim"], font=(UI_FONT, 8),
                 justify="left").pack(anchor="w", **pad)
        self.ignore_tags_var = tk.BooleanVar(value=self.options.get("ignore_tags", True))
        tk.Checkbutton(self, text="Ignorar o que estiver entre [ ]  (ex.: [refrão], igual ao Suno)",
                       variable=self.ignore_tags_var, bg=DARK["bg"], fg=DARK["text"],
                       activebackground=DARK["bg"], activeforeground=DARK["text"],
                       selectcolor=DARK["entry_bg"], font=(UI_FONT, 9)).pack(anchor="w", pady=(6, 0), padx=16)
        tf = tk.Frame(self, bg=DARK["border"], padx=1, pady=1)
        tf.pack(fill="both", expand=True, pady=(6, 0), **pad)
        self.lyrics_text = tk.Text(tf, bg=DARK["entry_bg"], fg=DARK["text"],
                                   insertbackground=DARK["text"], font=(UI_FONT, 10),
                                   relief="flat", bd=8, wrap="word", height=10)
        self.lyrics_text.pack(fill="both", expand=True)

        # Progress
        self.status_label = tk.Label(self, text="", bg=DARK["bg"], fg=DARK["text_dim"],
                                     font=(UI_FONT, 9))
        self.status_label.pack(anchor="w", pady=(10, 2), **pad)
        self._show_model_status()
        self.progress_bar = ttk.Progressbar(self, mode="determinate")
        self.progress_bar.pack(fill="x", **pad)

        # Buttons
        bf = tk.Frame(self, bg=DARK["bg"])
        bf.pack(fill="x", pady=14, **pad)
        self.cancel_btn = StyledButton(bf, "Cancelar", command=self._cancel, style="secondary")
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.gen_btn = StyledButton(bf, "🎤  Gerar", command=self._start)
        self.gen_btn.pack(side="right")

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
        if getattr(self, "_events", None) is not None:
            self.stop_worker()
        self.destroy()

    def _start(self):
        audio = self.project.audio_file
        if not audio or not os.path.isfile(audio):
            messagebox.showerror("Erro", "O arquivo de áudio não foi encontrado.", parent=self)
            return
        if not auto_lyrics.is_available():
            messagebox.showerror("Whisper não instalado",
                                 "A legenda automática precisa do faster-whisper.\n\n"
                                 "Feche e abra o programa de novo pra instalar, ou rode:\n"
                                 "    pip install faster-whisper", parent=self)
            return

        lyrics = self.lyrics_text.get("1.0", "end-1c")
        model = self._model_size()
        language = self.LANGUAGES.get(self.lang_var.get(), "pt")
        ignore_tags = self.ignore_tags_var.get()
        if self.on_options:
            self.on_options({"whisper_model": model, "language": self.lang_var.get(),
                             "ignore_tags": ignore_tags})

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

        def progress(p):
            self.post(lambda v=int(p * 100): self._set_progress(v))

        def run():
            try:
                result = auto_lyrics.generate(audio, lyrics, model, language,
                                              progress=progress,
                                              cancel=lambda: self.cancelled,
                                              ignore_tags=ignore_tags)
                outcome = (True, result)
            except auto_lyrics.Cancelled:
                return
            except auto_lyrics.AutoLyricsError as e:
                outcome = (False, str(e))
            except Exception as e:
                outcome = (False, f"Erro inesperado: {e}")
            if not self.cancelled:
                self.post(lambda: self._on_done(*outcome))

        self.start_worker(run)

    def _set_progress(self, pct: int):
        try:
            if str(self.progress_bar.cget("mode")) != "determinate":
                self.progress_bar.stop()
                self.progress_bar.config(mode="determinate")
            self.progress_bar["value"] = pct
            self.status_label.config(text=f"Ouvindo a música… {pct}%")
        except tk.TclError:
            pass

    def _reset_buttons(self):
        self.gen_btn.config(state="normal")
        self.cancel_btn.config(text="Cancelar")

    def _on_done(self, ok: bool, result):
        self.running = False
        if getattr(self, "_events", None) is not None:
            self.stop_worker()
        self.progress_bar.stop()
        self.progress_bar.config(mode="determinate")
        if not ok:
            self.status_label.config(text="❌  Erro ao gerar a legenda", fg=DARK["accent"])
            self._reset_buttons()
            messagebox.showerror("Erro", result, parent=self)
            return
        if not result:
            self._reset_buttons()
            messagebox.showwarning("Nada encontrado", "Nenhuma estrofe foi gerada.", parent=self)
            return
        if self.ask_before_replace and self.project.strophes and not messagebox.askyesno(
                "Substituir estrofes?",
                f"O projeto já tem {len(self.project.strophes)} estrofe(s).\n"
                f"Substituir pelas {len(result)} geradas?", parent=self):
            self._reset_buttons()
            return

        self.project.strophes = [Strophe(id=i + 1, start_time=s.start, end_time=s.end, text=s.text)
                                 for i, s in enumerate(result)]
        if self.on_done:
            self.on_done()
        messagebox.showinfo("Pronto",
                            f"{len(result)} estrofe(s) gerada(s)!\n\n"
                            "Confira os tempos e o texto antes de renderizar.", parent=self)
        self.destroy()


class RenderDialog(WorkerMixin, tk.Toplevel):
    """Progress window for rendering to `out_path` (starts right away)."""

    def __init__(self, parent, project: Project, out_path: str, autostart: bool = True):
        super().__init__(parent)
        self.project = project
        self.out_path = out_path
        self.cancelled = False
        self.title("Renderizando")
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
        if autostart:
            self.after(50, self._start_render)

    def _build(self):
        tk.Label(self, text="🎬  Renderizando vídeo", bg=DARK["bg"],
                 fg=DARK["text"], font=(UI_FONT, 13, "bold")).pack(anchor="w", pady=(20, 4), padx=24)
        tk.Label(self, text=self.out_path, bg=DARK["bg"], fg=DARK["text_dim"],
                 font=(UI_FONT, 8), wraplength=440, justify="left").pack(anchor="w", padx=24)

        self.progress_label = tk.Label(self, text="Preparando…", bg=DARK["bg"],
                                       fg=DARK["text_dim"], font=(UI_FONT, 9))
        self.progress_label.pack(anchor="w", padx=24, pady=(16, 4))
        self.progress_bar = ttk.Progressbar(self, length=440, mode="determinate")
        self.progress_bar.pack(padx=24)

        bf = tk.Frame(self, bg=DARK["bg"])
        bf.pack(fill="x", padx=24, pady=(18, 20))
        self.cancel_btn = StyledButton(bf, "⏹  Parar", command=self._cancel, style="secondary")
        self.cancel_btn.pack(side="right")

    def _cancel(self):
        self.cancelled = True
        if getattr(self, "_events", None) is not None:
            self.stop_worker()
        self.destroy()

    def _start_render(self):
        if self.cancelled:
            return
        try:
            renderer = FrameRenderer(self.project)
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível preparar o render:\n{e}", parent=self)
            self.destroy()
            return
        self.project.output_file = self.out_path
        out = self.out_path

        def progress(p):
            self.post(lambda v=int(p * 100): self._set_progress(v))

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
                self.post(lambda: self._on_done(ok, msg))

        self.start_worker(run)

    def _set_progress(self, pct: int):
        try:
            self.progress_bar["value"] = pct
            self.progress_label.config(text=f"Renderizando… {pct}%")
        except tk.TclError:
            pass

    def _on_done(self, ok: bool, msg: str):
        self.stop_worker()
        if ok:
            self.progress_label.config(text="✅  Pronto!", fg=DARK["success"])
            if messagebox.askyesno("Vídeo pronto", f"Vídeo salvo em:\n{msg}\n\nAbrir a pasta?",
                                   parent=self):
                open_folder(os.path.dirname(os.path.abspath(msg)))
            self.destroy()
        else:
            self.progress_label.config(text="❌  Erro na renderização", fg=DARK["accent"])
            self.cancel_btn.config(text="Fechar")
            messagebox.showerror("Erro", msg, parent=self)


class SongPanel(tk.Frame):
    """Left column: audio, title, background, the preview and the render button."""
    PREVIEW_W, PREVIEW_H = 400, 225
    THUMB_W, THUMB_H = 96, 54

    def __init__(self, parent, on_pick_audio=None, on_title_change=None, on_pick_bg=None,
                 on_clear_bg=None, on_render=None):
        super().__init__(parent, bg=DARK["bg"])
        self.on_title_change = on_title_change
        self._suspend_title_cb = False
        self._thumb = self._preview = None
        # Label sizes are in characters unless they hold an image: blank
        # images keep the boxes at a fixed pixel size while empty
        self._blank_thumb = tk.PhotoImage(master=self, width=self.THUMB_W, height=self.THUMB_H)
        self._blank_preview = tk.PhotoImage(master=self, width=self.PREVIEW_W, height=self.PREVIEW_H)

        # ── Song card ──────────────────────────────────
        card = tk.Frame(self, bg=DARK["surface"], padx=16, pady=14)
        card.pack(fill="x")
        section_title(card, "Música", DARK["surface"]).pack(anchor="w", pady=(0, 8))

        def row(label):
            r = tk.Frame(card, bg=DARK["surface"])
            r.pack(fill="x", pady=4)
            tk.Label(r, text=label, bg=DARK["surface"], fg=DARK["text_dim"],
                     font=(UI_FONT, 9), width=6, anchor="w").pack(side="left")
            return r

        r = row("Áudio")
        StyledButton(r, "Escolher…", command=on_pick_audio, style="secondary",
                     padx=10, pady=4, font=(UI_FONT, 9)).pack(side="right")
        self.audio_label = tk.Label(r, text="", bg=DARK["entry_bg"], fg=DARK["text"],
                                    font=(UI_FONT, 10), anchor="w", padx=8, pady=6)
        self.audio_label.pack(side="left", fill="x", expand=True, padx=(0, 6))

        r = row("Título")
        self.title_var = tk.StringVar()
        self.title_var.trace_add("write", self._title_written)
        self.title_entry = styled_entry(r, self.title_var, font_size=11)
        self.title_entry.pack(side="left", fill="x", expand=True)

        r = row("Fundo")
        self.bg_thumb = tk.Label(r, bg=DARK["entry_bg"], image=self._blank_thumb, bd=0,
                                 cursor="hand2")
        self.bg_thumb.pack(side="left")
        self.bg_thumb.bind("<Button-1>", lambda e: on_pick_bg and on_pick_bg())
        col = tk.Frame(r, bg=DARK["surface"])
        col.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.bg_label = tk.Label(col, text="", bg=DARK["surface"], fg=DARK["text_dim"],
                                 font=(UI_FONT, 9), anchor="w", justify="left", wraplength=230)
        self.bg_label.pack(anchor="w")
        btns = tk.Frame(col, bg=DARK["surface"])
        btns.pack(anchor="w", pady=(6, 0))
        StyledButton(btns, "Escolher…", command=on_pick_bg, style="secondary",
                     padx=10, pady=4, font=(UI_FONT, 9)).pack(side="left")
        self.bg_clear_btn = StyledButton(btns, "Remover", command=on_clear_bg, style="ghost",
                                         padx=10, pady=4, font=(UI_FONT, 9))
        self.bg_clear_btn.pack(side="left", padx=(6, 0))

        # ── Preview card ───────────────────────────────
        pcard = tk.Frame(self, bg=DARK["surface"], padx=16, pady=14)
        pcard.pack(fill="x", pady=(12, 0))
        section_title(pcard, "Prévia", DARK["surface"]).pack(anchor="w", pady=(0, 8))
        self.preview_label = tk.Label(pcard, bg="#000000", image=self._blank_preview, bd=0)
        self.preview_label.pack()
        self.preview_caption = tk.Label(pcard, text="", bg=DARK["surface"], fg=DARK["text_dim"],
                                        font=(UI_FONT, 9))
        self.preview_caption.pack(anchor="w", pady=(8, 0))

        # ── Render ─────────────────────────────────────
        self.render_btn = StyledButton(self, "▶   Renderizar vídeo", command=on_render,
                                       font=(UI_FONT, 12, "bold"), pady=12)
        self.render_btn.pack(fill="x", side="bottom", pady=(12, 0))

    # ── Setters (called by App) ─────────────────────────

    def set_audio(self, path: str):
        self.audio_label.config(text=os.path.basename(path) if path else "Nenhum áudio escolhido",
                                fg=DARK["text"] if path else DARK["text_dim"])

    def set_title(self, title: str):
        self._suspend_title_cb = True
        self.title_var.set(title)
        self._suspend_title_cb = False

    def _title_written(self, *_):
        if not self._suspend_title_cb and self.on_title_change:
            self.on_title_change(self.title_var.get())

    def set_background(self, path: str, hint: str):
        self._thumb = None
        if path and HAS_PIL:
            try:
                img = Image.open(path).convert("RGB")
                img = img.resize((self.THUMB_W, self.THUMB_H), Image.LANCZOS)
                self._thumb = ImageTk.PhotoImage(img, master=self)
            except Exception:
                self._thumb = None
        self.bg_thumb.config(image=self._thumb or self._blank_thumb)
        self.bg_label.config(text=os.path.basename(path) if path else hint,
                             fg=DARK["text"] if path else DARK["text_dim"])
        if path:
            self.bg_clear_btn.pack(side="left", padx=(6, 0))
        else:
            self.bg_clear_btn.pack_forget()

    def show_preview(self, img, caption: str):
        self._preview = ImageTk.PhotoImage(img, master=self) if img is not None else None
        self.preview_label.config(image=self._preview or self._blank_preview)
        self.preview_caption.config(text=caption)


def _checkerboard(w: int, h: int, size: int = 10):
    """Gray checkerboard to show where the video is transparent."""
    img = Image.new("RGBA", (w, h), (44, 44, 54, 255))
    d = ImageDraw.Draw(img)
    for y in range(0, h, size):
        for x in range((y // size) % 2 * size, w, size * 2):
            d.rectangle((x, y, x + size - 1, y + size - 1), fill=(60, 60, 72, 255))
    return img


def render_preview(project: Project, text: str, w: int, h: int):
    """
    How a strophe looks in the video (title + text over the background),
    scaled to w×h. Transparent backgrounds are shown over a checkerboard.
    """
    p = replace(project, strophes=[Strophe(0, 0.0, 10.0, text or " ")], fade_duration=0.0)
    renderer = FrameRenderer(p)
    frame = renderer.render_frame(5.0).resize((w, h), Image.LANCZOS)
    if renderer._transparent:
        frame = Image.alpha_composite(_checkerboard(w, h), frame)
    return frame.convert("RGB")


class App(tk.Tk):
    def __init__(self, config_file: Optional[str] = None):
        super().__init__()
        self.title("LyricRenderer")
        self.configure(bg=DARK["bg"])
        self.geometry("1280x800")
        self.minsize(1040, 660)

        self.config_file = config_file or config_path()
        self.cfg = load_config(self.config_file)
        self.project = self._fresh_project()
        self._current_file = None
        self._lyrics_audio = None   # audio the current strophes were made for
        self._preview_job = None

        self._set_icon()
        self._setup_styles()
        self._build_ui()
        self._bind_keys()
        self._sync_song_panel()
        self._on_project_change()

    # ── Setup ─────────────────────────────────

    def _fresh_project(self) -> Project:
        """New project with the saved style and the last background image."""
        p = Project(title="")
        apply_style(p, self.cfg)
        bg = self.cfg.get("bg_image", "")
        if isinstance(bg, str) and bg and os.path.isfile(bg):
            p.bg_image = bg
        return p

    def _save_cfg(self, **changes):
        self.cfg.update(changes)
        save_config(self.cfg, self.config_file)

    def _set_icon(self):
        ico = os.path.join(_default_assets_dir(), "icon.ico")
        png = os.path.join(_default_assets_dir(), "icon.png")
        try:
            if sys.platform == "win32" and os.path.isfile(ico):
                self.iconbitmap(default=ico)
            elif os.path.isfile(png):
                self._icon_png = tk.PhotoImage(master=self, file=png)
                self.iconphoto(True, self._icon_png)
        except tk.TclError:
            pass

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        for name in ("TScrollbar", "Vertical.TScrollbar"):
            style.configure(name, background=DARK["surface2"], troughcolor=DARK["bg"],
                            bordercolor=DARK["bg"], lightcolor=DARK["surface2"],
                            darkcolor=DARK["surface2"], arrowcolor=DARK["text_dim"],
                            gripcount=0, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", DARK["border"])])
        style.configure("Horizontal.TProgressbar", background=DARK["accent"],
                        troughcolor=DARK["surface"], bordercolor=DARK["surface"])
        style.configure("TCombobox", fieldbackground=DARK["entry_bg"],
                        background=DARK["surface2"], foreground=DARK["text"],
                        arrowcolor=DARK["text"], bordercolor=DARK["border"])
        # readonly comboboxes were drawn gray
        style.map("TCombobox",
                  fieldbackground=[("readonly", DARK["entry_bg"])],
                  foreground=[("readonly", DARK["text"])],
                  selectbackground=[("readonly", DARK["entry_bg"])],
                  selectforeground=[("readonly", DARK["text"])])
        self.option_add("*TCombobox*Listbox.background", DARK["entry_bg"])
        self.option_add("*TCombobox*Listbox.foreground", DARK["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", DARK["accent"])

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=DARK["surface"], height=60)
        header.pack(fill="x")
        header.pack_propagate(False)
        png = os.path.join(_default_assets_dir(), "icon.png")
        try:
            self._logo = tk.PhotoImage(master=self, file=png).subsample(8)  # 256 → 32 px
            tk.Label(header, image=self._logo, bg=DARK["surface"]).pack(side="left", padx=(20, 10))
        except tk.TclError:
            self._logo = None
        tk.Label(header, text="LyricRenderer", bg=DARK["surface"], fg=DARK["text"],
                 font=(UI_FONT, 15, "bold")).pack(side="left", padx=(0 if self._logo else 20, 0))

        StyledButton(header, "⚙  Configurações", command=self._open_settings,
                     style="ghost").pack(side="right", padx=(4, 16))
        StyledButton(header, "💾  Salvar", command=self._save_project,
                     style="ghost").pack(side="right", padx=4)
        StyledButton(header, "📂  Abrir", command=self._open_project,
                     style="ghost").pack(side="right", padx=4)

        # Status bar
        self.statusbar = tk.Label(self, text="", bg=DARK["surface"], fg=DARK["text_dim"],
                                  font=(UI_FONT, 8), anchor="w", padx=16, pady=5)
        self.statusbar.pack(fill="x", side="bottom")

        # Body: song panel | strophes
        body = tk.Frame(self, bg=DARK["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=16)

        self.song = SongPanel(body, on_pick_audio=self._pick_audio,
                              on_title_change=self._title_changed,
                              on_pick_bg=self._pick_bg, on_clear_bg=self._clear_bg,
                              on_render=self._render)
        self.song.pack(side="left", fill="y")

        self.strophe_list = StropheList(body, self.project,
                                        on_change=self._on_project_change,
                                        on_auto_lyrics=self._auto_lyrics,
                                        on_select=lambda sid: self._schedule_preview())
        self.strophe_list.pack(side="left", fill="both", expand=True, padx=(16, 0))

    def _bind_keys(self):
        for key, fn in (("s", self._save_project), ("r", self._render),
                        ("o", self._open_project), ("n", self._new_project)):
            self.bind_all(f"<Control-{key}>", lambda e, f=fn: f())
            self.bind_all(f"<Control-{key.upper()}>", lambda e, f=fn: f())  # caps lock

    def report_callback_exception(self, exc, val, tb):
        """Errors in buttons/callbacks: show them (no console with the shortcut)."""
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            log = os.path.join(os.path.dirname(self.config_file), "erros.log")
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            log = ""
        messagebox.showerror("Erro inesperado",
                             f"{val}\n\nDetalhes salvos em:\n{log}" if log else str(val))

    # ── Song panel ────────────────────────────

    def _sync_song_panel(self):
        p = self.project
        self.song.set_audio(p.audio_file)
        self.song.set_title(p.title)
        self._sync_background()

    def _sync_background(self):
        p = self.project
        bg = p.bg_image if p.bg_image and os.path.isfile(p.bg_image) else ""
        hint = ("Sem imagem: fundo transparente (.webm)" if p.transparent_bg
                else "Sem imagem: fundo de cor sólida")
        self.song.set_background(bg, hint)

    def _pick_audio(self):
        path = filedialog.askopenfilename(
            parent=self, title="Escolher áudio da música",
            initialdir=self.cfg.get("audio_dir") or None,
            filetypes=[("Áudio", "*.mp3 *.wav *.ogg *.aac *.m4a *.flac"), ("Todos", "*.*")])
        if path:
            self.set_audio(path)

    def set_audio(self, path: str):
        """New song: remember the audio and fill the title from the file name."""
        self.project.audio_file = path
        self.song.set_audio(path)
        self.song.set_title(title_from_filename(path))
        self.project.title = self.song.title_var.get()
        self._save_cfg(audio_dir=os.path.dirname(path))
        self._on_project_change()

    def _title_changed(self, title: str):
        self.project.title = title
        self._on_project_change()

    def _pick_bg(self):
        current = self.project.bg_image
        path = filedialog.askopenfilename(
            parent=self, title="Escolher imagem de fundo",
            initialdir=os.path.dirname(current) if current else None,
            filetypes=[("Imagem", "*.png *.jpg *.jpeg *.bmp *.webp"), ("Todos", "*.*")])
        if path:
            self.set_background(path)

    def set_background(self, path: str):
        self.project.bg_image = path
        self._save_cfg(bg_image=path)
        self._sync_background()
        self._on_project_change()

    def _clear_bg(self):
        self.set_background("")

    # ── Settings / preview / status ────────────

    def _open_settings(self):
        SettingsDialog(self, self.project, on_saved=self._settings_saved)

    def _settings_saved(self):
        self._save_cfg(**style_of(self.project))
        self._sync_background()
        self._on_project_change()

    def _on_project_change(self):
        p = self.project
        n = len(p.strophes)
        name = os.path.basename(self._current_file) if self._current_file else "projeto não salvo"
        end = max((s.end_time for s in p.strophes), default=0)
        self.statusbar.config(
            text=f"{name}   ·   {n} estrofe{'s' if n != 1 else ''}"
                 + (f"   ·   legenda até {format_time(end)}" if n else ""))
        self._schedule_preview()

    def _schedule_preview(self):
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(120, self._update_preview)

    def _selected_strophe(self) -> Optional[Strophe]:
        strophes = self.strophe_list.sorted_strophes()
        sid = self.strophe_list.selected_id
        return next((s for s in strophes if s.id == sid), strophes[0] if strophes else None)

    def _update_preview(self):
        self._preview_job = None
        if not HAS_PIL:
            return
        s = self._selected_strophe()
        strophes = self.strophe_list.sorted_strophes()
        try:
            img = render_preview(self.project, s.text if s else "",
                                 SongPanel.PREVIEW_W, SongPanel.PREVIEW_H)
        except Exception:
            img = None
        if s:
            idx = strophes.index(s) + 1
            caption = (f"Estrofe {idx} de {len(strophes)}   ·   "
                       f"{format_time(s.start_time)} → {format_time(s.end_time)}")
        else:
            caption = "As estrofes aparecem aqui. Clique numa estrofe pra ver como fica."
        self.song.show_preview(img, caption)

    # ── File operations ───────────────────────

    def _new_project(self):
        if messagebox.askyesno("Novo projeto", "Descartar o projeto atual e começar outro?"):
            self._load_project(self._fresh_project(), None)

    def _load_project(self, project: Project, path: Optional[str]):
        """Swap the current project and refresh everything."""
        self.project = project
        self._current_file = path
        self._lyrics_audio = project.audio_file if project.strophes else None
        self.strophe_list.project = project
        self.strophe_list.selected_id = None
        self.strophe_list.refresh()
        self._sync_song_panel()
        self._on_project_change()

    def _open_project(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[
            ("LyricRenderer", "*.lyr"), ("JSON", "*.json"), ("Todos", "*.*")])
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
            parent=self, defaultextension=".lyr",
            filetypes=[("LyricRenderer", "*.lyr"), ("JSON", "*.json")],
            initialfile=f"{safe_filename(self.project.title)}.lyr"
        )
        if path:
            self._current_file = path
            self._do_save(path)

    def _do_save(self, path: str):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.project.to_dict(), f, ensure_ascii=False, indent=2)
            self._on_project_change()
            self.statusbar.config(text=f"Salvo em {path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível salvar:\n{e}")

    # ── Auto lyrics / render ───────────────────

    def _auto_lyrics(self):
        p = self.project
        if not p.audio_file or not os.path.isfile(p.audio_file):
            messagebox.showinfo("Escolha o áudio", "Escolha o áudio da música primeiro "
                                                   "(em Música → Áudio).")
            return

        def on_done():
            self._lyrics_audio = p.audio_file
            self.strophe_list.selected_id = None
            self.strophe_list.refresh()
            self._on_project_change()

        # Strophes left from another song are replaced without asking
        ask = bool(p.strophes) and self._lyrics_audio in (None, p.audio_file)
        AutoLyricsDialog(self, p, options=self.cfg,
                         on_options=lambda o: self._save_cfg(**o),
                         on_done=on_done, ask_before_replace=ask)

    def _render(self):
        if not self.project.strophes:
            messagebox.showwarning("Aviso", "Adicione ao menos uma estrofe antes de renderizar.")
            return
        out = self._ask_output_path()
        if out:
            RenderDialog(self, self.project, out)

    def _ask_output_path(self) -> Optional[str]:
        transparent = project_is_transparent(self.project)
        ext = ".webm" if transparent else ".mp4"
        types = ([("WebM transparente", "*.webm")] if transparent else
                 [("Vídeo MP4", "*.mp4"), ("WebM", "*.webm")])
        initial_dir = self.cfg.get("output_dir")
        if not (initial_dir and os.path.isdir(initial_dir)):
            videos = os.path.join(os.path.expanduser("~"), "Videos")
            initial_dir = videos if os.path.isdir(videos) else os.path.expanduser("~")
        path = filedialog.asksaveasfilename(
            parent=self, title="Salvar vídeo como", initialdir=initial_dir,
            initialfile=f"{safe_filename(self.project.title)}{ext}",
            defaultextension=ext, filetypes=types + [("Todos", "*.*")],
            confirmoverwrite=False)  # we ask ourselves (also covers .mp4 → .webm)
        if not path:
            return None
        return self._confirm_output(path, transparent)

    def _confirm_output(self, path: str, transparent: bool) -> Optional[str]:
        """Final output path, after asking before overwriting an existing file."""
        final = final_output_path(path, transparent)
        if os.path.exists(final) and not messagebox.askyesno(
                "Arquivo já existe",
                f"Já existe um arquivo com esse nome:\n{final}\n\nSubstituir?",
                icon="warning", default="no", parent=self):
            return None
        self._save_cfg(output_dir=os.path.dirname(os.path.abspath(final)))
        return final

    def check_ffmpeg(self):
        status = deps.ffmpeg_status()
        if status != "ok":
            messagebox.showwarning("FFmpeg", deps.FFMPEG_HELP[status], parent=self)


# ─────────────────────────────────────────────
#  Startup
# ─────────────────────────────────────────────

def _ensure_std_streams():
    """pythonw (the shortcut) has no console: some libraries crash writing to it."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _set_windows_app_id():
    """Makes the taskbar show our icon instead of Python's."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("dantas.lyricrenderer")
        except Exception:
            pass


def _restart():
    subprocess.Popen([sys.executable, os.path.abspath(__file__), *sys.argv[1:]])


def check_dependencies() -> bool:
    """
    Offer to install missing packages. Returns True to go on opening the app,
    False to quit (the app restarts itself after installing).
    """
    required = deps.missing(deps.REQUIRED)
    optional = deps.missing(deps.OPTIONAL)
    broken_pil = not required and not HAS_PIL

    if not required and not optional and not broken_pil:
        return True

    root = tk.Tk()
    root.withdraw()
    ico = os.path.join(_default_assets_dir(), "icon.ico")
    if sys.platform == "win32" and os.path.isfile(ico):
        try:
            root.iconbitmap(default=ico)
        except tk.TclError:
            pass

    if broken_pil:
        messagebox.showerror("LyricRenderer", "O Pillow está instalado, mas não carregou:\n\n"
                                              f"{PIL_ERROR}")
        root.destroy()
        return False

    lines = [f"• {pkg} — pra {deps.REQUIRED.get(pkg, deps.OPTIONAL.get(pkg))[1]}"
             + ("" if pkg in deps.REQUIRED else " (opcional)")
             for pkg in required + optional]
    if not messagebox.askyesno("Instalar dependências",
                               "Faltam alguns componentes:\n\n" + "\n".join(lines)
                               + "\n\nInstalar agora? (precisa de internet)"):
        root.destroy()
        if required:
            return False  # can't run without them
        return True

    win = tk.Toplevel(root)
    win.title("Instalando…")
    win.configure(bg=DARK["bg"], padx=24, pady=20)
    win.resizable(False, False)
    tk.Label(win, text="Instalando, pode levar alguns minutos…", bg=DARK["bg"],
             fg=DARK["text"], font=(UI_FONT, 10)).pack(anchor="w")
    bar = ttk.Progressbar(win, mode="indeterminate", length=360)
    bar.pack(pady=(12, 0))
    bar.start(12)
    result = {}

    def work():
        result["ok"], result["out"] = deps.pip_install(required + optional)

    t = threading.Thread(target=work, daemon=True)
    t.start()

    def wait():
        if t.is_alive():
            root.after(200, wait)
        else:
            root.quit()
    root.after(200, wait)
    root.mainloop()
    win.destroy()

    if result.get("ok"):
        messagebox.showinfo("Pronto", "Instalado! O programa vai abrir de novo.")
        root.destroy()
        _restart()
        return False
    messagebox.showerror("Erro ao instalar",
                         "Não deu pra instalar:\n\n" + result.get("out", "")[-800:])
    root.destroy()
    return not required


def main():
    _ensure_std_streams()
    _set_windows_app_id()
    if not check_dependencies():
        return
    app = App()
    app.after(800, app.check_ffmpeg)
    app.mainloop()


if __name__ == "__main__":
    main()
