"""
Creates a "LyricRenderer" shortcut on the Windows Desktop that opens the app
with a double click (no terminal window), using assets/icon.ico.

Run again if you move the project folder or reinstall Python:
    python tools/create_shortcut.py
"""
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def pythonw_path() -> str:
    """pythonw.exe runs a script without opening a console window."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def create(name: str = "LyricRenderer") -> str:
    if sys.platform != "win32":
        raise SystemExit("O atalho é só pro Windows.")
    script = os.path.join(ROOT, "lyric_renderer.py")
    icon = os.path.join(ROOT, "assets", "icon.ico")

    def q(s):  # PowerShell single-quoted string
        return "'" + s.replace("'", "''") + "'"

    ps = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$path = Join-Path $desktop {q(name + '.lnk')}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$s.TargetPath = {q(pythonw_path())}
$s.Arguments = {q('"' + script + '"')}
$s.WorkingDirectory = {q(ROOT)}
$s.IconLocation = {q(icon + ',0')}
$s.Description = 'Gerador de vídeos de letras de música'
$s.Save()
Write-Output $path
"""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"Não deu pra criar o atalho:\n{out.stderr}")
    return out.stdout.strip()


if __name__ == "__main__":
    print("Atalho criado em:", create())
