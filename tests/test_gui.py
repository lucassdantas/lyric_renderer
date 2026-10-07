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

    def _auto_dialog(self):
        self.app._auto_lyrics()
        return next(w for w in self.app.winfo_children() if isinstance(w, lr.AutoLyricsDialog))

    def test_auto_lyrics_fills_the_strophe_list(self):
        dialog = self._auto_dialog()
        result = [lr.auto_lyrics.TimedStrophe(1.2, 5.5, "linha 1\nlinha 2"),
                  lr.auto_lyrics.TimedStrophe(6.0, 9.0, "linha 3")]
        with mock.patch.object(lr.messagebox, "showinfo"):
            dialog._on_done(True, result)
        self.assertEqual([(s.id, s.start_time, s.end_time, s.text) for s in self.app.project.strophes],
                         [(1, 1.2, 5.5, "linha 1\nlinha 2"), (2, 6.0, 9.0, "linha 3")])
        self.assertIs(self.app.strophe_list.project, self.app.project)
        cards = self.app.strophe_list.scrollable_frame.winfo_children()
        self.assertEqual(len(cards), 2)

    def test_auto_lyrics_asks_before_replacing(self):
        self.app.project.strophes.append(lr.Strophe(1, 0, 2, "minha"))
        dialog = self._auto_dialog()
        with mock.patch.object(lr.messagebox, "askyesno", return_value=False):
            dialog._on_done(True, [lr.auto_lyrics.TimedStrophe(1, 2, "nova")])
        self.assertEqual([s.text for s in self.app.project.strophes], ["minha"])
        dialog.destroy()

    def test_auto_lyrics_error_is_shown(self):
        dialog = self._auto_dialog()
        with mock.patch.object(lr.messagebox, "showerror") as err:
            dialog._on_done(False, "deu ruim")
        err.assert_called_once()
        self.assertEqual(str(dialog.gen_btn.cget("state")), "normal")
        dialog.destroy()

    def test_auto_lyrics_needs_audio(self):
        dialog = self._auto_dialog()
        dialog.audio_var.set("")
        with mock.patch.object(lr.messagebox, "showerror") as err:
            dialog._start()
        err.assert_called_once()
        self.assertFalse(dialog.running)
        dialog.destroy()

    def test_auto_lyrics_runs_in_background(self):
        # the whole flow with Whisper replaced by a fake
        audio = os.path.join(self.tmp.name, "song.wav")
        open(audio, "wb").close()
        dialog = self._auto_dialog()
        dialog.audio_var.set(audio)
        dialog.lyrics_text.insert("1.0", "oi\n\ntchau")
        fake = [lr.auto_lyrics.TimedStrophe(1, 2, "oi"), lr.auto_lyrics.TimedStrophe(3, 4, "tchau")]
        with mock.patch.object(lr.auto_lyrics, "is_available", return_value=True), \
             mock.patch.object(lr.auto_lyrics, "generate", return_value=fake) as gen, \
             mock.patch.object(lr.messagebox, "showinfo"):
            dialog._start()
            for _ in range(200):
                self.app.update()
                if not dialog.winfo_exists():
                    break
                self.app.after(10)
        args = gen.call_args
        self.assertEqual(args.args[:2], (audio, "oi\n\ntchau"))
        self.assertEqual(self.app.project.audio_file, audio)  # reused for the render
        self.assertEqual(self.app.settings.audio_var.get(), audio)
        self.assertEqual([s.text for s in self.app.project.strophes], ["oi", "tchau"])

    def _card_buttons(self, card):
        top = card.winfo_children()[1].winfo_children()[0]
        return {w.cget("text"): w for w in top.winfo_children() if isinstance(w, tk.Button)}

    def test_merge_button_joins_with_next(self):
        sl = self.app.strophe_list
        self.app.project.strophes = [lr.Strophe(1, 0, 4, "v1\nv2"), lr.Strophe(2, 5, 9, "v3\nv4"),
                                     lr.Strophe(3, 10, 14, "pre1\npre2")]
        sl.refresh()
        cards = sl.scrollable_frame.winfo_children()
        self.assertIn("↓ Juntar", self._card_buttons(cards[0]))
        self.assertNotIn("↓ Juntar", self._card_buttons(cards[-1]))  # nothing below the last

        self._card_buttons(cards[0])["↓ Juntar"].invoke()
        self.assertEqual([(s.start_time, s.end_time, s.text) for s in self.app.project.strophes],
                         [(0, 9, "v1\nv2\nv3\nv4"), (10, 14, "pre1\npre2")])
        self.assertEqual(len(sl.scrollable_frame.winfo_children()), 2)

    def _wheel(self, widget, delta):
        widget.event_generate("<MouseWheel>", delta=delta, when="now")
        self.app.update()

    def test_wheel_scrolls_strophe_list_over_cards(self):
        sl = self.app.strophe_list
        self.app.project.strophes = [lr.Strophe(i, i * 10, i * 10 + 5, f"linha {i}\noutra")
                                     for i in range(1, 31)]
        sl.refresh()
        self.app.deiconify()  # needs real geometry to know there's something to scroll
        self.app.update()
        self.assertLess(sl.canvas.yview()[1], 1.0)

        card = sl.scrollable_frame.winfo_children()[0]
        lyric_label = [w for w in card.winfo_children()[1].winfo_children()
                       if isinstance(w, tk.Label)][0]
        self._wheel(lyric_label, -120)  # wheel down over the lyric text
        self.assertGreater(sl.canvas.yview()[0], 0)
        self._wheel(lyric_label, 120)
        self.assertEqual(sl.canvas.yview()[0], 0)

    def test_wheel_elsewhere_does_not_scroll_list(self):
        sl = self.app.strophe_list
        self.app.project.strophes = [lr.Strophe(i, i * 10, i * 10 + 5, "x\ny\nz")
                                     for i in range(1, 31)]
        sl.refresh()
        self.app.deiconify()
        self.app.update()
        self._wheel(self.app.statusbar, -120)
        self.assertEqual(sl.canvas.yview()[0], 0)

    def test_wheel_scrolls_settings_panel_over_fields(self):
        self.app.geometry("1000x400")  # small window, so the panel overflows
        self.app.deiconify()
        self.app.update()
        sp = self.app.settings
        canvas = sp._inner.master
        self.assertLess(canvas.yview()[1], 1.0)
        label = sp._bg_image_row.winfo_children()[0]
        self._wheel(label, -120)
        self.assertGreater(canvas.yview()[0], 0)

    def test_save_writes_project(self):
        path = os.path.join(self.tmp.name, "s.lyr")
        self.app.settings.title_var.set("Salvo")
        self.app._do_save(path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["title"], "Salvo")


if __name__ == "__main__":
    unittest.main()
