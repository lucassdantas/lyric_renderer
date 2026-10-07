"""
Dependency checks done when the app starts (replaces the old run.py).

- Python packages: checked with find_spec (no import), installed with pip
  after the user agrees.
- FFmpeg: can't be installed from Python, so we only tell the user what's
  wrong (missing, or blocked by Windows Smart App Control).
"""

import importlib.util
import subprocess
import sys

# pip package -> (import name, why it is needed)
REQUIRED = {"Pillow": ("PIL", "desenhar os quadros do vídeo")}
OPTIONAL = {"faster-whisper": ("faster_whisper", "gerar a legenda automática")}


def _flags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def missing(packages: dict) -> list:
    """pip names of the packages that are not installed."""
    return [pkg for pkg, (module, _why) in packages.items()
            if importlib.util.find_spec(module) is None]


def pip_install(pkgs: list):
    """Install packages with this same Python. Returns (ok, output)."""
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pkgs]
    out = ""
    for cmd in (base, base + ["--break-system-packages"]):  # 2nd: Linux "externally managed"
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_flags())
        except OSError as e:
            return False, str(e)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            return True, out
        if "externally-managed" not in out:
            break
    return False, out


def ffmpeg_status() -> str:
    """'ok', 'missing', 'blocked' (Windows Smart App Control) or 'error'."""
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, creationflags=_flags())
        return "ok" if r.returncode == 0 else "error"
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        return "blocked" if getattr(e, "winerror", None) == 4551 else "error"


FFMPEG_HELP = {
    "missing": ("O FFmpeg não foi encontrado.\n\nSem ele dá pra montar a legenda, mas não "
                "dá pra renderizar o vídeo.\n\nBaixe em https://www.gyan.dev/ffmpeg/builds/ "
                "e coloque a pasta 'bin' no PATH do Windows."),
    "blocked": ("O Windows está bloqueando o FFmpeg (Controle inteligente de aplicativos / "
                "Smart App Control).\n\nSem ele não dá pra renderizar o vídeo.\n\n"
                "Veja em: Segurança do Windows → Controle de aplicativos e do navegador."),
    "error": "O FFmpeg foi encontrado, mas não funcionou. Teste rodando 'ffmpeg -version'.",
}
