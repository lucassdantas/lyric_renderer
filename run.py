#!/usr/bin/env python3
"""
LyricRenderer — Instalador/Lançador
Verifica dependências e inicia o app.
"""
import subprocess
import sys
import os

# pip package name -> import name
REQUIRED = {"Pillow": "PIL"}

def check_deps():
    missing = []
    for pkg, module in REQUIRED.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg)
    return missing

def install(pkgs):
    base = [sys.executable, "-m", "pip", "install", *pkgs]
    try:
        subprocess.check_call(base)
    except subprocess.CalledProcessError:
        # Linux distros with an "externally managed" Python refuse plain installs
        subprocess.check_call(base + ["--break-system-packages"])

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

    # Launch app. subprocess instead of os.execv: on Windows execv breaks
    # when the Python path has spaces (e.g. "C:\Program Files\...").
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lyric_renderer.py")
    sys.exit(subprocess.call([sys.executable, script]))
