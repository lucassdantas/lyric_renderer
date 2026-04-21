#!/usr/bin/env python3
"""
LyricRenderer — Instalador/Lançador
Verifica dependências e inicia o app.
"""
import subprocess
import sys
import os

REQUIRED = ["Pillow"]

def check_deps():
    missing = []
    for pkg in REQUIRED:
        try:
            __import__(pkg.lower().replace("-", "_"))
        except ImportError:
            missing.append(pkg)
    return missing

def install(pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", *pkgs])

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True)
        return True
    except FileNotFoundError:
        return False

if __name__ == "__main__":
    missing = check_deps()
    if missing:
        print(f"Instalando dependências: {missing}")
        try:
            install(missing)
        except Exception as e:
            print(f"Erro ao instalar: {e}")
            print(f"Instale manualmente: pip install {' '.join(missing)}")
            sys.exit(1)

    if not check_ffmpeg():
        print("⚠️  FFmpeg não encontrado!")
        print("Instale o FFmpeg:")
        print("  Ubuntu/Debian: sudo apt install ffmpeg")
        print("  macOS: brew install ffmpeg")
        print("  Windows: https://ffmpeg.org/download.html")
        input("Pressione Enter para continuar mesmo assim...")

    # Launch app
    script = os.path.join(os.path.dirname(__file__), "lyric_renderer.py")
    os.execv(sys.executable, [sys.executable, script])
