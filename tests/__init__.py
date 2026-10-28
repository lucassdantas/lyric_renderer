"""
Tests. Real dialogs must never open while testing: they'd pop up on the
user's screen (they may be using the PC) and block until clicked. Every
messagebox/file dialog is replaced by one that fails the test; tests that
expect a dialog mock it explicitly (mock.patch.object(lr.messagebox, ...)).
"""
import tkinter.colorchooser as _colorchooser
import tkinter.filedialog as _filedialog
import tkinter.messagebox as _messagebox


def _blocked(name):
    def dialog(*args, **kwargs):
        raise AssertionError(f"diálogo real aberto durante o teste: {name}{args[:2]}")
    return dialog


for _name in ("showinfo", "showwarning", "showerror", "askyesno", "askyesnocancel",
              "askokcancel", "askquestion", "askretrycancel"):
    setattr(_messagebox, _name, _blocked(_name))
for _name in ("askopenfilename", "askopenfilenames", "asksaveasfilename", "askdirectory"):
    setattr(_filedialog, _name, _blocked(_name))
_colorchooser.askcolor = _blocked("askcolor")
