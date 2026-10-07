"""GUI regressions, driven headlessly (window withdrawn, dialogs stubbed)."""
import json
import os
import tempfile
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

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
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        self.app = lr.App(config_file=self.cfg_path)
        self.app.withdraw()

    def tearDown(self):
        self.app.destroy()
        self.tmp.cleanup()

    def _file(self, name, content=b""):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def _image(self, name="bg.png"):
        path = os.path.join(self.tmp.name, name)
        Image.new("RGB", (64, 36), (200, 30, 60)).save(path)
        return path

    def _dialog(self, cls):
        return next(w for w in self.app.winfo_children() if isinstance(w, cls))

    # ── Song panel ───────────────────────────────────────

    def test_choosing_audio_fills_the_title(self):
        audio = self._file("03_onde_eu_fui (1).mp3")
        self.app.set_audio(audio)
        self.assertEqual(self.app.project.audio_file, audio)
        self.assertEqual(self.app.project.title, "Onde Eu Fui")
        self.assertEqual(self.app.song.title_var.get(), "Onde Eu Fui")
        self.assertEqual(self.app.song.audio_label.cget("text"), "03_onde_eu_fui (1).mp3")

    def test_title_can_be_edited(self):
        self.app.set_audio(self._file("onde_eu_fui.mp3"))
        self.app.song.title_var.set("Onde Eu Fui (Ao Vivo)")
        self.assertEqual(self.app.project.title, "Onde Eu Fui (Ao Vivo)")

    def test_background_is_remembered_for_next_time(self):
        bg = self._image()
        self.app.set_background(bg)
        self.assertEqual(self.app.project.bg_image, bg)
        self.assertEqual(self.app.song.bg_label.cget("text"), "bg.png")
        self.assertEqual(lr.load_config(self.cfg_path)["bg_image"], bg)

        other = lr.App(config_file=self.cfg_path)  # next launch
        try:
            self.assertEqual(other.project.bg_image, bg)
        finally:
            other.destroy()

    def test_remove_background(self):
        self.app.set_background(self._image())
        self.app._clear_bg()
        self.assertEqual(self.app.project.bg_image, "")
        self.assertEqual(lr.load_config(self.cfg_path)["bg_image"], "")

    def test_preview_shows_selected_strophe(self):
        self.app.project.strophes = [lr.Strophe(1, 0, 4, "um"), lr.Strophe(2, 5, 9, "dois")]
        self.app.strophe_list.refresh()
        self.app.strophe_list.select(2)
        self.app._update_preview()
        self.assertIn("Estrofe 2 de 2", self.app.song.preview_caption.cget("text"))
        self.assertIsNotNone(self.app.song._preview)
        self.assertEqual((self.app.song._preview.width(), self.app.song._preview.height()),
                         (lr.SongPanel.PREVIEW_W, lr.SongPanel.PREVIEW_H))

    def test_preview_without_strophes(self):
        self.app._update_preview()
        self.assertIn("Clique numa estrofe", self.app.song.preview_caption.cget("text"))

    def test_clicking_a_card_selects_it(self):
        sl = self.app.strophe_list
        self.app.project.strophes = [lr.Strophe(1, 0, 4, "um"), lr.Strophe(2, 5, 9, "dois")]
        sl.refresh()
        self.app.deiconify()  # clicks only reach visible windows
        self.app.update()
        self.assertEqual(sl.selected_id, 1)  # first one by default
        second = sl.scrollable_frame.winfo_children()[1]
        lyric = [w for w in second.winfo_children()[1].winfo_children() if isinstance(w, tk.Label)][0]
        lyric.event_generate("<Button-1>")
        self.assertEqual(sl.selected_id, 2)
        self.assertEqual(second.cget("highlightbackground"), lr.DARK["selected"])

    # ── Settings window ──────────────────────────────────

    def _settings(self):
        self.app._open_settings()
        return self._dialog(lr.SettingsDialog)

    def test_settings_are_saved_for_next_time(self):
        d = self._settings()
        d.panel.lyric_size_var.set(55)
        d.panel.text_color_var.set("#ffcc00")
        d._save()
        self.assertFalse(d.winfo_exists())
        self.assertEqual(self.app.project.lyric_size, 55)
        cfg = lr.load_config(self.cfg_path)
        self.assertEqual((cfg["lyric_size"], cfg["text_color"]), (55, "#ffcc00"))

        other = lr.App(config_file=self.cfg_path)
        try:
            self.assertEqual((other.project.lyric_size, other.project.text_color), (55, "#ffcc00"))
        finally:
            other.destroy()

    def test_settings_cancel_changes_nothing(self):
        d = self._settings()
        d.panel.lyric_size_var.set(11)
        d.destroy()
        self.assertEqual(self.app.project.lyric_size, 67)
        self.assertNotIn("lyric_size", lr.load_config(self.cfg_path))

    def test_invalid_number_is_reported(self):
        d = self._settings()
        d.panel.fps_var.set("")  # e.g. user cleared the field
        with mock.patch.object(lr.messagebox, "showerror") as err:
            d._save()
            err.assert_called_once()
        self.assertTrue(d.winfo_exists())  # stays open to fix it
        d.destroy()

    def test_transparent_toggle_shows_bg_color_row(self):
        sp = self._settings().panel
        sp.transparent_var.set(True)
        sp._on_transparent_toggle()
        self.assertFalse(sp._bg_color_row.winfo_manager())
        sp.transparent_var.set(False)
        sp._on_transparent_toggle()
        self.assertEqual(sp._bg_color_row.winfo_manager(), "pack")
        rows = sp._inner.pack_slaves()
        self.assertEqual(rows.index(sp._transparent_row) + 1, rows.index(sp._bg_color_row))

    # ── Project files ────────────────────────────────────

    def test_open_project_keeps_its_settings(self):
        proj = lr.Project(title="Minha Música", lyric_size=40, text_color="#ff0000",
                          transparent_bg=False, video_width=1280, video_height=720,
                          strophes=[lr.Strophe(1, 0, 2, "a")])
        path = os.path.join(self.tmp.name, "p.lyr")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(proj.to_dict(), f, ensure_ascii=False)

        with mock.patch.object(lr.filedialog, "askopenfilename", return_value=path):
            self.app._open_project()

        self.assertEqual(self.app.project, proj)
        self.assertEqual(self.app.song.title_var.get(), "Minha Música")
        self.assertIs(self.app.strophe_list.project, self.app.project)
        # the settings window shows the opened project's values
        d = self._settings()
        self.assertEqual(d.panel.res_var.get(), "1280x720")
        d._save()
        self.assertEqual(self.app.project, proj)

    def test_new_project_keeps_style_and_background(self):
        bg = self._image()
        self.app.set_background(bg)
        self.app.project.lyric_size = 50
        self.app._save_cfg(**lr.style_of(self.app.project))
        self.app.set_audio(self._file("a.mp3"))
        self.app.project.strophes.append(lr.Strophe(1, 0, 2, "a"))
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True):
            self.app._new_project()
        p = self.app.project
        self.assertEqual((p.strophes, p.audio_file, p.title), ([], "", ""))
        self.assertEqual((p.bg_image, p.lyric_size), (bg, 50))
        self.assertIs(self.app.strophe_list.project, p)

    def test_save_writes_project(self):
        path = os.path.join(self.tmp.name, "s.lyr")
        self.app.song.title_var.set("Salvo")
        self.app._do_save(path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["title"], "Salvo")

    # ── Render ───────────────────────────────────────────

    def test_render_asks_before_overwriting(self):
        out = self._file("Onde Eu Fui.mp4", b"old video")
        with mock.patch.object(lr.messagebox, "askyesno", return_value=False) as ask:
            self.assertIsNone(self.app._confirm_output(out, transparent=False))
            ask.assert_called_once()
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True):
            self.assertEqual(self.app._confirm_output(out, transparent=False), out)
        self.assertEqual(lr.load_config(self.cfg_path)["output_dir"], self.tmp.name)

    def test_render_new_file_doesnt_ask(self):
        out = os.path.join(self.tmp.name, "novo.mp4")
        with mock.patch.object(lr.messagebox, "askyesno") as ask:
            self.assertEqual(self.app._confirm_output(out, transparent=False), out)
            ask.assert_not_called()

    def test_render_checks_the_webm_that_will_really_be_written(self):
        # transparent video: user picked .mp4, the file written is .webm
        self._file("clip.webm", b"old")
        with mock.patch.object(lr.messagebox, "askyesno", return_value=False) as ask:
            self.assertIsNone(self.app._confirm_output(os.path.join(self.tmp.name, "clip.mp4"),
                                                       transparent=True))
            ask.assert_called_once()

    def test_render_dialog_flow(self):
        self.app.project.strophes = [lr.Strophe(1, 0, 1, "x")]
        out = os.path.join(self.tmp.name, "v.mp4")
        with mock.patch.object(lr.FrameRenderer, "render_video", return_value=(True, out)) as rv, \
             mock.patch.object(lr.messagebox, "askyesno", return_value=False):
            d = lr.RenderDialog(self.app, self.app.project, out)
            for _ in range(100):
                self.app.update()
                if not d.winfo_exists():
                    break
                self.app.after(20)
        self.assertEqual(rv.call_args.args[0], out)
        self.assertEqual(self.app.project.output_file, out)

    # ── Strophe editor / list ────────────────────────────

    def _open_new_strophe_editor(self):
        sl = self.app.strophe_list
        sl._add_strophe()
        return next(w for w in sl.winfo_children() if isinstance(w, lr.StropheEditor))

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
        self.assertEqual(self.app.strophe_list.selected_id, self.app.project.strophes[-1].id)

    def test_new_strophe_after_delete(self):
        self.app.project.strophes += [lr.Strophe(1, 0, 10, "a"), lr.Strophe(2, 11, 20, "b"),
                                      lr.Strophe(3, 21, 30, "c")]
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True):
            self.app.strophe_list._delete(3)
        editor = self._open_new_strophe_editor()
        self.assertEqual((editor.start_entry.var.get(), editor.end_entry.var.get()),
                         ("00:21", "00:30"))
        editor.destroy()

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

    # ── Auto lyrics ──────────────────────────────────────

    def _auto_dialog(self, audio=True):
        if audio:
            self.app.set_audio(self._file("song.wav"))
        self.app._auto_lyrics()
        return self._dialog(lr.AutoLyricsDialog)

    def test_auto_lyrics_needs_audio_first(self):
        with mock.patch.object(lr.messagebox, "showinfo") as info:
            self.app._auto_lyrics()
        info.assert_called_once()
        self.assertFalse([w for w in self.app.winfo_children() if isinstance(w, lr.AutoLyricsDialog)])

    def test_auto_lyrics_fills_the_strophe_list(self):
        dialog = self._auto_dialog()
        result = [lr.auto_lyrics.TimedStrophe(1.2, 5.5, "linha 1\nlinha 2"),
                  lr.auto_lyrics.TimedStrophe(6.0, 9.0, "linha 3")]
        with mock.patch.object(lr.messagebox, "showinfo"):
            dialog._on_done(True, result)
        self.assertEqual([(s.id, s.start_time, s.end_time, s.text) for s in self.app.project.strophes],
                         [(1, 1.2, 5.5, "linha 1\nlinha 2"), (2, 6.0, 9.0, "linha 3")])
        self.assertEqual(len(self.app.strophe_list.scrollable_frame.winfo_children()), 2)

    def test_auto_lyrics_asks_before_replacing_same_song(self):
        dialog = self._auto_dialog()
        with mock.patch.object(lr.messagebox, "showinfo"):
            dialog._on_done(True, [lr.auto_lyrics.TimedStrophe(1, 2, "primeira")])
        dialog = self._auto_dialog(audio=False)  # same song again
        with mock.patch.object(lr.messagebox, "askyesno", return_value=False):
            dialog._on_done(True, [lr.auto_lyrics.TimedStrophe(1, 2, "nova")])
        self.assertEqual([s.text for s in self.app.project.strophes], ["primeira"])
        dialog.destroy()

    def test_auto_lyrics_replaces_previous_song_without_asking(self):
        dialog = self._auto_dialog()
        with mock.patch.object(lr.messagebox, "showinfo"):
            dialog._on_done(True, [lr.auto_lyrics.TimedStrophe(1, 2, "música 1")])
        self.app.set_audio(self._file("musica2.wav"))  # next song
        dialog = self._auto_dialog(audio=False)
        with mock.patch.object(lr.messagebox, "askyesno") as ask, \
             mock.patch.object(lr.messagebox, "showinfo"):
            dialog._on_done(True, [lr.auto_lyrics.TimedStrophe(1, 2, "música 2")])
        ask.assert_not_called()
        self.assertEqual([s.text for s in self.app.project.strophes], ["música 2"])

    def test_auto_lyrics_error_is_shown(self):
        dialog = self._auto_dialog()
        with mock.patch.object(lr.messagebox, "showerror") as err:
            dialog._on_done(False, "deu ruim")
        err.assert_called_once()
        self.assertEqual(str(dialog.gen_btn.cget("state")), "normal")
        dialog.destroy()

    def test_auto_lyrics_shows_if_model_is_downloaded(self):
        downloaded = {"small"}
        with mock.patch.object(lr.auto_lyrics, "is_downloaded", side_effect=lambda m: m in downloaded):
            dialog = self._auto_dialog()
            self.assertIn("já baixado", dialog.status_label.cget("text"))
            dialog.model_var.set(lr.auto_lyrics.MODELS["medium"])
            dialog._show_model_status()
            self.assertIn("~1,5 GB", dialog.status_label.cget("text"))
        dialog.destroy()

    def test_auto_lyrics_runs_in_background_and_remembers_options(self):
        dialog = self._auto_dialog()
        audio = self.app.project.audio_file
        dialog.lyrics_text.insert("1.0", "[refrão]\noi\n\ntchau")
        dialog.ignore_tags_var.set(False)
        dialog.model_var.set(lr.auto_lyrics.MODELS["medium"])
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
        self.assertEqual(args.args[:3], (audio, "[refrão]\noi\n\ntchau", "medium"))
        self.assertEqual(args.kwargs["ignore_tags"], False)
        self.assertEqual([s.text for s in self.app.project.strophes], ["oi", "tchau"])
        cfg = lr.load_config(self.cfg_path)
        self.assertEqual((cfg["ignore_tags"], cfg["whisper_model"]), (False, "medium"))

        dialog = self._auto_dialog(audio=False)  # next time: same choices
        self.assertFalse(dialog.ignore_tags_var.get())
        self.assertEqual(dialog.model_var.get(), lr.auto_lyrics.MODELS["medium"])
        dialog.destroy()

    # ── Mouse wheel ──────────────────────────────────────

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

    def test_wheel_scrolls_settings_over_fields(self):
        self.app.deiconify()  # the dialog is only visible with its parent
        d = self._settings()
        d.geometry("520x300")  # small window, so the panel overflows
        d.update()
        sp = d.panel
        canvas = sp._inner.master
        self.assertLess(canvas.yview()[1], 1.0)
        label = sp._transparent_row.winfo_children()[0]
        label.event_generate("<MouseWheel>", delta=-120, when="now")
        d.update()
        self.assertGreater(canvas.yview()[0], 0)
        d.destroy()


@unittest.skipUnless(HAS_TK, "Tk sem display disponível")
class DependencyCheckTest(unittest.TestCase):
    def test_nothing_missing_opens_directly(self):
        with mock.patch.object(lr.deps, "missing", return_value=[]), \
             mock.patch.object(lr.messagebox, "askyesno") as ask:
            self.assertTrue(lr.check_dependencies())
        ask.assert_not_called()

    def test_declining_optional_still_opens(self):
        def missing(group):
            return ["faster-whisper"] if group is lr.deps.OPTIONAL else []
        with mock.patch.object(lr.deps, "missing", side_effect=missing), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=False), \
             mock.patch.object(lr.deps, "pip_install") as pip:
            self.assertTrue(lr.check_dependencies())
        pip.assert_not_called()

    def test_declining_required_quits(self):
        def missing(group):
            return ["Pillow"] if group is lr.deps.REQUIRED else []
        with mock.patch.object(lr.deps, "missing", side_effect=missing), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=False):
            self.assertFalse(lr.check_dependencies())

    def test_install_then_restart(self):
        def missing(group):
            return ["faster-whisper"] if group is lr.deps.OPTIONAL else []
        with mock.patch.object(lr.deps, "missing", side_effect=missing), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=True), \
             mock.patch.object(lr.messagebox, "showinfo"), \
             mock.patch.object(lr.deps, "pip_install", return_value=(True, "ok")) as pip, \
             mock.patch.object(lr, "_restart") as restart:
            self.assertFalse(lr.check_dependencies())  # this process ends…
        pip.assert_called_once_with(["faster-whisper"])
        restart.assert_called_once()                    # …and a new one opens

    def test_install_failure_is_shown(self):
        def missing(group):
            return ["faster-whisper"] if group is lr.deps.OPTIONAL else []
        with mock.patch.object(lr.deps, "missing", side_effect=missing), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=True), \
             mock.patch.object(lr.messagebox, "showerror") as err, \
             mock.patch.object(lr.deps, "pip_install", return_value=(False, "sem internet")):
            self.assertTrue(lr.check_dependencies())  # optional: opens anyway
        self.assertIn("sem internet", err.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
