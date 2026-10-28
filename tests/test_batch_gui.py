"""📚 Lote screen, driven headlessly (window withdrawn, dialogs and heavy work mocked)."""
import os
import tempfile
import tkinter as tk
import unittest
from unittest import mock

import batch
import lyric_renderer as lr
from tests.test_gui import HAS_TK


def fake_generate(audio, lyrics, model, language, progress=None, cancel=None, ignore_tags=True, **kw):
    progress and progress(0.5)
    name = os.path.splitext(os.path.basename(audio))[0]
    return [lr.auto_lyrics.TimedStrophe(1.0, 5.0, f"{name} verso 1\n{name} verso 2",
                                        [lr.auto_lyrics.Word(w, 1 + i * 0.5, 1.4 + i * 0.5)
                                         for i, w in enumerate(f"{name} verso 1 {name} verso 2".split())]),
            lr.auto_lyrics.TimedStrophe(6.0, 9.0, f"{name} refrão")]


@unittest.skipUnless(HAS_TK, "Tk sem display disponível")
class BatchScreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg_path = os.path.join(self.dir, "cfg", "config.json")
        self.out = os.path.join(self.dir, "videos")
        os.makedirs(self.out)
        self.app = self._new_app()
        self.view = self.app.batch_view

    def tearDown(self):
        self.app.destroy()
        self.tmp.cleanup()

    def _new_app(self):
        app = lr.App(config_file=self.cfg_path)
        app.withdraw()
        return app

    def _file(self, name, content=""):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _add(self, *names):
        paths = [self._file(n) for n in names]
        with mock.patch.object(lr.filedialog, "askopenfilenames", return_value=paths), \
             mock.patch.object(lr.messagebox, "showinfo"):
            self.app.batch_add()
        return paths

    def _wait(self, cond, limit=300):
        for _ in range(limit):
            self.app.update()
            if cond():
                return
            self.app.after(10)
        self.fail("timed out waiting for the batch")

    def _run_until_done(self):
        self._wait(lambda: not self.app.batch_running)

    # ── Screen ──────────────────────────────────────────

    def test_tabs_switch_screens(self):
        self.app.show_view("batch")
        self.assertEqual(self.view.winfo_manager(), "pack")
        self.assertFalse(self.app.body.winfo_manager())
        self.assertIn("Lote:", self.app.statusbar.cget("text"))
        self.app.show_view("single")
        self.assertFalse(self.view.winfo_manager())
        self.assertEqual(self.app.body.winfo_manager(), "pack")

    def test_empty_batch_shows_a_hint(self):
        self.app.show_view("batch")
        self.assertEqual(self.view.empty_label.winfo_manager(), "pack")
        self.assertFalse(self.view.form.winfo_manager())

    def test_add_audios_with_titles_and_txt_lyrics(self):
        self._file("sorriso_solto.txt", "[estrofe]\nSem olhar pra trás")
        self._add("onde_eu_fui_2.mp3", "sorriso_solto.mp3")
        items = self.app.batch_items
        self.assertEqual([i.title for i in items], ["Onde Eu Fui", "Sorriso Solto"])
        self.assertEqual(items[1].lyrics, "[estrofe]\nSem olhar pra trás")
        self.app.show_view("batch")
        self.assertEqual(len(self.view.rows_frame.winfo_children()), 2)
        self.assertEqual(self.view.form.winfo_manager(), "pack")

    def test_editing_title_and_lyrics_updates_the_song(self):
        self._add("a.mp3", "b.mp3")
        self.app.show_view("batch")
        self.view.select(1)
        self.view.title_var.set("Música B")
        self.view.lyrics_text.insert("1.0", "linha 1\nlinha 2")
        self.app.update()
        self.assertEqual(self.app.batch_items[1].title, "Música B")
        self.assertEqual(self.app.batch_items[1].lyrics, "linha 1\nlinha 2")
        self.assertEqual(self.app.batch_items[0].lyrics, "")
        self.view.select(0)  # switching songs doesn't leak the text into the other one
        self.assertEqual(self.view.lyrics_text.get("1.0", "end-1c"), "")
        self.assertEqual(self.app.batch_items[1].lyrics, "linha 1\nlinha 2")

    def test_batch_survives_closing_the_app(self):
        self._add("a.mp3")
        self.app.batch_items[0].lyrics = "letra"
        self.app.batch_folder = self.out
        self.app.batch_changed()
        self.app.destroy()  # saves on the way out
        self.app = self._new_app()
        self.assertEqual([(i.title, i.lyrics) for i in self.app.batch_items], [("A", "letra")])
        self.assertEqual(self.app.batch_folder, self.out)
        self.view = self.app.batch_view

    def test_remove_and_clear(self):
        self._add("a.mp3", "b.mp3", "c.mp3")
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True):
            self.app.batch_remove(1)
            self.assertEqual([i.title for i in self.app.batch_items], ["A", "C"])
            self.app.batch_clear()
        self.assertEqual(self.app.batch_items, [])

    def test_left_column_fits_while_reviewing(self):
        self._add("a.mp3")
        self.app.batch_items[0].set_strophes([{"id": 1, "start_time": 0, "end_time": 2, "text": "x"}])
        self.app.open_review(0)
        self.app.update_idletasks()
        available = (800 - 60 - self.app.statusbar.winfo_reqheight() - 32
                     - self.app.review_bar.winfo_reqheight() - 12)
        self.assertLessEqual(self.app.song.winfo_reqheight(), available)

    # ── Generate / render ───────────────────────────────

    def test_generate_lyrics_for_every_song(self):
        self._add("um.mp3", "dois.mp3")
        self.app.batch_items[0].lyrics = "um verso 1\num verso 2\n\num refrão"
        self.app.show_view("batch")
        with mock.patch.object(lr.auto_lyrics, "is_available", return_value=True), \
             mock.patch.object(lr.auto_lyrics, "generate", side_effect=fake_generate) as gen, \
             mock.patch.object(lr.messagebox, "showinfo") as info:
            self.app.batch_generate()
            self.assertTrue(self.app.batch_running)
            self.assertEqual(self.view.stop_btn.winfo_manager(), "pack")
            self._run_until_done()
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(gen.call_args_list[0].args[1], "um verso 1\num verso 2\n\num refrão")
        self.assertEqual([i.status for i in self.app.batch_items], [batch.LYRICS] * 2)
        first = self.app.batch_items[0].strophes[0]
        self.assertEqual(first["text"], "um verso 1\num verso 2")
        self.assertEqual(first["words"][0], ["um", 1.0, 1.4])  # karaoke timing kept
        self.assertIn("Revisar", info.call_args.args[1])
        self.assertFalse(self.view.stop_btn.winfo_manager())
        # saved for next time
        saved, _ = batch.load_batch(self.app.batch_file)
        self.assertEqual(saved[0].strophes, self.app.batch_items[0].strophes)

    def test_generate_needs_whisper(self):
        self._add("um.mp3")
        with mock.patch.object(lr.auto_lyrics, "is_available", return_value=False), \
             mock.patch.object(lr.messagebox, "showerror") as err:
            self.app.batch_generate()
        err.assert_called_once()
        self.assertFalse(self.app.batch_running)

    def test_missing_audio_is_reported(self):
        paths = self._add("um.mp3")
        os.remove(paths[0])
        with mock.patch.object(lr.messagebox, "showerror") as err:
            self.app.batch_generate()
        self.assertIn("Um", err.call_args.args[1])

    def _with_lyrics(self, *names):
        self._add(*names)
        for it in self.app.batch_items:
            it.set_strophes([{"id": 1, "start_time": 0, "end_time": 2, "text": it.title, "words": []}])
        self.app.batch_folder = self.out

    def _render_all(self, answer=True, overwrite=None):
        calls = []

        def render_video(renderer, out, audio_path="", progress_callback=None, cancel_flag=None):
            calls.append((renderer.proj.title, out, audio_path))
            return True, out
        with mock.patch.object(lr.FrameRenderer, "render_video", render_video), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=answer), \
             mock.patch.object(lr.messagebox, "askyesnocancel", return_value=overwrite) as ask, \
             mock.patch.object(lr.messagebox, "showinfo"):
            self.app.batch_render()
            self._run_until_done()
        return calls, ask

    def test_render_all_to_the_folder(self):
        self._with_lyrics("um.mp3", "dois.mp3")
        calls, ask = self._render_all()
        self.assertEqual([(c[0], c[1]) for c in calls],
                         [("Um", os.path.join(self.out, "Um.mp4")),
                          ("Dois", os.path.join(self.out, "Dois.mp4"))])
        self.assertTrue(calls[0][2].endswith("um.mp3"))
        ask.assert_not_called()  # nothing to overwrite
        self.assertEqual([i.status for i in self.app.batch_items], [batch.DONE] * 2)

    def test_render_uses_saved_style_and_background(self):
        self._with_lyrics("um.mp3")
        bg = os.path.join(self.dir, "bg.png")
        lr.Image.new("RGB", (32, 18)).save(bg)
        self.app.set_background(bg)
        self.app._save_cfg(lyric_size=55)
        seen = []
        with mock.patch.object(lr.FrameRenderer, "render_video",
                               lambda r, out, **kw: (seen.append(r.proj), (True, out))[1]), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=True), \
             mock.patch.object(lr.messagebox, "showinfo"):
            self.app.batch_render()
            self._run_until_done()
        self.assertEqual((seen[0].bg_image, seen[0].lyric_size, seen[0].mode), (bg, 55, "video"))

    def test_unreviewed_songs_are_confirmed_first(self):
        self._with_lyrics("um.mp3")
        calls, _ = self._render_all(answer=False)
        self.assertEqual(calls, [])
        self.app.batch_items[0].reviewed = True
        with mock.patch.object(lr.messagebox, "askyesno") as ask:
            calls, _ = self._render_all()
        self.assertEqual(len(calls), 1)

    def test_existing_videos_replace_or_skip(self):
        self._with_lyrics("um.mp3", "dois.mp3")
        self._file(os.path.join("videos", "Um.mp4"), "velho")
        calls, ask = self._render_all(overwrite=False)          # Não = pular
        ask.assert_called_once()
        self.assertEqual([c[0] for c in calls], ["Dois"])
        self.assertEqual(self.app.batch_items[0].status, batch.LYRICS)
        calls, _ = self._render_all(overwrite=True)              # Sim = substituir
        self.assertEqual([c[0] for c in calls], ["Um"])
        for it in self.app.batch_items:
            it.rendered = False
        calls, _ = self._render_all(overwrite=None)              # Cancelar
        self.assertEqual(calls, [])

    def test_render_asks_for_the_folder(self):
        self._with_lyrics("um.mp3")
        self.app.batch_folder = ""
        with mock.patch.object(lr.messagebox, "showinfo"), \
             mock.patch.object(lr.messagebox, "askyesno", return_value=True), \
             mock.patch.object(lr.filedialog, "askdirectory", return_value="") as pick:
            self.app.batch_render()
        pick.assert_called_once()
        self.assertFalse(self.app.batch_running)

    def test_direct_generates_and_renders(self):
        self._add("um.mp3", "dois.mp3")
        self.app.batch_folder = self.out
        rendered = []
        with mock.patch.object(lr.auto_lyrics, "is_available", return_value=True), \
             mock.patch.object(lr.auto_lyrics, "generate", side_effect=fake_generate), \
             mock.patch.object(lr.FrameRenderer, "render_video",
                               lambda r, out, **kw: (rendered.append(r.proj.strophes[0].text), (True, out))[1]), \
             mock.patch.object(lr.messagebox, "showinfo"):
            self.app.batch_direct()
            self._run_until_done()
        self.assertEqual(rendered, ["um verso 1\num verso 2", "dois verso 1\ndois verso 2"])
        self.assertEqual([i.status for i in self.app.batch_items], [batch.DONE] * 2)

    def test_stop(self):
        self._add("um.mp3", "dois.mp3")

        def slow_generate(audio, *a, cancel=None, **kw):
            while not cancel():
                pass
            raise lr.auto_lyrics.Cancelled()
        with mock.patch.object(lr.auto_lyrics, "is_available", return_value=True), \
             mock.patch.object(lr.auto_lyrics, "generate", side_effect=slow_generate):
            self.app.batch_generate()
            self.app.batch_stop()
            self._run_until_done()
        self.assertEqual([i.status for i in self.app.batch_items], [batch.PENDING] * 2)
        self.assertIn("Parado", self.view.progress_label.cget("text"))

    def test_regenerate_and_render_one(self):
        self._with_lyrics("um.mp3", "dois.mp3")
        self.app.batch_items[1].reviewed = True
        with mock.patch.object(lr.messagebox, "askyesno", return_value=True), \
             mock.patch.object(lr.auto_lyrics, "is_available", return_value=True), \
             mock.patch.object(lr.auto_lyrics, "generate", side_effect=fake_generate) as gen, \
             mock.patch.object(lr.messagebox, "showinfo"):
            self.app.batch_regenerate(1)
            self._run_until_done()
        self.assertEqual(gen.call_count, 1)
        self.assertFalse(self.app.batch_items[1].reviewed)
        calls = []
        with mock.patch.object(lr.FrameRenderer, "render_video",
                               lambda r, out, **kw: (calls.append(r.proj.title), (True, out))[1]), \
             mock.patch.object(lr.messagebox, "showinfo"):
            self.app.batch_render_one(0)
            self._run_until_done()
        self.assertEqual(calls, ["Um"])

    # ── Review ──────────────────────────────────────────

    def test_review_edit_and_go_to_the_next(self):
        self._with_lyrics("um.mp3", "dois.mp3", "tres.mp3")
        self.app.batch_items[1].reviewed = True
        single = self.app.project
        single.title = "Meu projeto único"

        self.app.open_review(0)
        self.assertEqual(self.app.project.title, "Um")
        self.assertEqual(self.app.view, "single")
        self.assertEqual(self.app.review_bar.winfo_manager(), "pack")
        self.assertIn("1 de 3", self.app.review_label.cget("text"))
        # edit in the normal editor
        self.app.project.strophes[0].text = "Um corrigido"
        self.app.song.title_var.set("Um (Versão Final)")

        self.app.finish_review(mark=True, go_next=True)
        first = self.app.batch_items[0]
        self.assertEqual((first.title, first.strophes[0]["text"], first.reviewed),
                         ("Um (Versão Final)", "Um corrigido", True))
        self.assertEqual(self.app.project.title, "Tres")  # skipped "Dois" (already reviewed)

        with mock.patch.object(lr.messagebox, "showinfo") as info:
            self.app.finish_review(mark=True, go_next=True)
        info.assert_called_once()  # everything reviewed
        self.assertEqual(self.app.view, "batch")
        self.assertFalse(self.app.review_bar.winfo_manager())
        self.assertIs(self.app.project, single)  # the single-song project is back

    def test_editing_a_rendered_song_needs_a_new_render(self):
        self._with_lyrics("um.mp3")
        self.app.batch_items[0].rendered = True
        self.app.open_review(0)
        self.app.finish_review(mark=False, go_next=False)
        self.assertTrue(self.app.batch_items[0].rendered)   # nothing changed
        self.app.open_review(0)
        self.app.project.strophes[0].end_time = 3
        self.app.finish_review(mark=False, go_next=False)
        self.assertFalse(self.app.batch_items[0].rendered)

    def test_batch_tab_while_reviewing_saves_and_goes_back(self):
        self._with_lyrics("um.mp3")
        self.app.open_review(0)
        self.app.project.strophes[0].text = "novo"
        self.app.show_view("batch")
        self.assertEqual(self.app.batch_items[0].strophes[0]["text"], "novo")
        self.assertIsNone(self.app._reviewing)
        self.assertEqual(self.app.view, "batch")

    def test_no_shorts_while_reviewing(self):
        self._with_lyrics("um.mp3")
        self.app.open_review(0)
        with mock.patch.object(lr.messagebox, "showinfo") as info:
            self.app.set_mode("short")
        info.assert_called_once()
        self.assertEqual(self.app.project.mode, "video")

    def test_songs_without_lyrics_cant_be_reviewed(self):
        self._add("um.mp3")
        self.app.open_review(0)
        self.assertIsNone(self.app._reviewing)

    def test_ctrl_r_renders_the_batch_on_the_batch_screen(self):
        self.app.show_view("batch")
        with mock.patch.object(self.app, "batch_render") as br, \
             mock.patch.object(self.app, "_render") as single:
            self.app._render_shortcut()
        br.assert_called_once()
        single.assert_not_called()


if __name__ == "__main__":
    unittest.main()
