"""GUI regressions, driven headlessly (window withdrawn, dialogs stubbed)."""
import json
import os
import tempfile
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

import lyric_renderer as lr

try:
    _root = tk.Tk()
    _root.destroy()
    HAS_TK = True
except tk.TclError:
    HAS_TK = False


def key(keysym, char="", state=0):
    return SimpleNamespace(keysym=keysym, char=char, state=state)


def type_into(time_entry, text):
    """Simulate clicking into a TimeEntry and typing."""
    time_entry._on_focus_in()
    results = [time_entry._on_key(key(c, c)) for c in text]
    return results


@unittest.skipUnless(HAS_TK, "Tk sem display disponível")
class TimeEntryTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.t = lr.TimeEntry(self.root, 21)

    def tearDown(self):
        self.root.destroy()

    def test_bank_style_mask(self):
        self.assertEqual(self.t.var.get(), "00:21")
        shown = []
        self.t._on_focus_in()
        for c in "1305":
            self.t._on_key(key(c, c))
            shown.append(self.t.var.get())
        self.assertEqual(shown, ["00:01", "00:13", "01:30", "13:05"])
        self.assertEqual(self.t.get_seconds(), 13 * 60 + 5)

    def test_first_digit_after_focus_replaces_value(self):
        type_into(self.t, "45")
        self.assertEqual(self.t.var.get(), "00:45")

    def test_max_four_digits(self):
        type_into(self.t, "99599")
        self.assertEqual(self.t.var.get(), "99:59")

    def test_letters_and_symbols_are_blocked(self):
        results = type_into(self.t, "1a:.")
        self.assertEqual(self.t.var.get(), "00:01")
        self.assertTrue(all(r == "break" for r in results))

    def test_backspace_and_delete(self):
        type_into(self.t, "130")
        self.t._on_key(key("BackSpace"))
        self.assertEqual(self.t.var.get(), "00:13")
        self.t._on_key(key("Delete"))
        self.assertEqual(self.t.var.get(), "00:00")

    def test_arrows_step_one_second(self):
        self.t._on_key(key("Up"))
        self.assertEqual(self.t.var.get(), "00:22")
        self.t.step(-1)
        self.t.step(-1)
        self.assertEqual(self.t.var.get(), "00:20")
        self.t.set_seconds(59)
        self.t.step(1)
        self.assertEqual(self.t.var.get(), "01:00")
        self.t.set_seconds(0)
        self.t.step(-1)
        self.assertEqual(self.t.var.get(), "00:00")

    def test_invalid_seconds_are_red_then_normalized(self):
        type_into(self.t, "190")
        self.assertEqual(self.t.var.get(), "01:90")
        self.assertFalse(self.t.is_valid())
        self.assertEqual(self.t.entry.cget("fg"), lr.DARK["accent"])
        self.t._on_focus_out()
        self.assertEqual(self.t.var.get(), "02:30")
        self.assertEqual(self.t.get_seconds(), 150)

    def test_paste_keeps_only_digits(self):
        self.root.clipboard_clear()
        self.root.clipboard_append("01:30")
        self.t._on_paste()
        self.assertEqual(self.t.var.get(), "01:30")

    def test_control_shortcuts_pass_through(self):
        self.assertIsNone(self.t._on_key(key("s", "\x13", state=0x4)))
        self.assertIsNone(self.t._on_key(key("Tab")))

    def test_untouched_fraction_is_kept(self):
        # old projects may have 21.5s: don't lose it if the field isn't edited
        self.t.set_seconds(21.5)
        self.assertEqual(self.t.var.get(), "00:21")
        self.assertEqual(self.t.get_seconds(), 21.5)
        self.t.step(1)
        self.assertEqual(self.t.get_seconds(), 22)


@unittest.skipUnless(HAS_TK, "Tk sem display disponível")
class AppTest(unittest.TestCase):
    def setUp(self):
        self.app = lr.App()
        self.app.withdraw()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.app.destroy()
        self.tmp.cleanup()

    def test_transparent_toggle_shows_bg_color_row(self):
        sp = self.app.settings
        sp.transparent_var.set(True)
        sp._on_transparent_toggle()
        self.assertFalse(sp._bg_color_row.winfo_manager())
        sp.transparent_var.set(False)
        sp._on_transparent_toggle()  # used to raise "isn't packed"
        self.assertEqual(sp._bg_color_row.winfo_manager(), "pack")
        # and it lands right above the background image row
        rows = sp._inner.pack_slaves()
        self.assertEqual(rows.index(sp._bg_color_row) + 1, rows.index(sp._bg_image_row))

    def test_open_project_keeps_its_settings(self):
        proj = lr.Project(title="Minha Música", lyric_size=40, text_color="#ff0000",
                          transparent_bg=False, video_width=1280, video_height=720,
                          strophes=[lr.Strophe(1, 0, 2, "a")])
        path = os.path.join(self.tmp.name, "p.lyr")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(proj.to_dict(), f, ensure_ascii=False)

        with mock.patch.object(lr.filedialog, "askopenfilename", return_value=path):
            self.app._open_project()

        self.assertEqual(self.app.project.title, "Minha Música")
        self.assertEqual(self.app.project.lyric_size, 40)
        sp = self.app.settings
        self.assertEqual(sp.title_var.get(), "Minha Música")
        self.assertEqual(sp.res_var.get(), "1280x720")
        # applying the form again must not revert anything
        self.assertTrue(sp._apply())
        self.assertEqual(self.app.project, proj)
        self.assertIs(self.app.strophe_list.project, self.app.project)

    def test_new_project_keeps_ui_working(self):
        self.app.project.strophes.append(lr.Strophe(1, 0, 2, "a"))
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True):
            self.app._new_project()
        self.assertEqual(self.app.project.strophes, [])
        self.assertTrue(self.app.settings.winfo_exists())
        self.assertTrue(self.app.strophe_list.winfo_exists())
        self.assertIs(self.app.settings.project, self.app.project)
        self.assertIs(self.app.strophe_list.project, self.app.project)

    def test_invalid_number_is_reported(self):
        sp = self.app.settings
        sp.fps_var.set("")  # e.g. user cleared the field
        with mock.patch.object(lr.messagebox, "showerror") as err:
            self.assertFalse(sp._apply())
            err.assert_called_once()

    def _open_new_strophe_editor(self):
        sl = self.app.strophe_list
        sl._add_strophe()
        editor = next(w for w in sl.winfo_children() if isinstance(w, lr.StropheEditor))
        return editor

    def test_new_strophe_starts_after_last_one(self):
        self.app.project.strophes += [lr.Strophe(1, 0, 10, "a"), lr.Strophe(2, 11, 20, "b")]
        editor = self._open_new_strophe_editor()
        self.assertEqual((editor.start_entry.var.get(), editor.end_entry.var.get()),
                         ("00:21", "00:30"))
        # still editable, and the same start as the previous end is allowed
        type_into(editor.start_entry, "20")
        editor.text_widget.insert("1.0", "c")
        editor._save()
        self.assertEqual(self.app.project.strophes[-1].start_time, 20)

    def test_new_strophe_after_delete(self):
        self.app.project.strophes += [lr.Strophe(1, 0, 10, "a"), lr.Strophe(2, 11, 20, "b"),
                                      lr.Strophe(3, 21, 30, "c")]
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True):
            self.app.strophe_list._delete(3)
        editor = self._open_new_strophe_editor()
        self.assertEqual((editor.start_entry.var.get(), editor.end_entry.var.get()),
                         ("00:21", "00:30"))
        editor.destroy()

    def test_save_writes_project(self):
        path = os.path.join(self.tmp.name, "s.lyr")
        self.app.settings.title_var.set("Salvo")
        self.app._do_save(path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["title"], "Salvo")


if __name__ == "__main__":
    unittest.main()
