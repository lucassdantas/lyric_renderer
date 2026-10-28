"""Batch logic (batch.py): items, lyrics files, autosave and the queue runner."""
import json
import os
import tempfile
import threading
import unittest

import batch
from batch import BatchItem, BatchRunner


def title(path):
    return os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name, content="", encoding="utf-8"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding=encoding) as f:
            f.write(content)
        return path


class StatusTest(unittest.TestCase):
    def test_status_follows_the_item(self):
        it = BatchItem("a.mp3", "A")
        self.assertEqual(it.status, batch.PENDING)
        it.set_strophes([{"id": 1, "start_time": 0, "end_time": 1, "text": "x"}])
        self.assertEqual(it.status, batch.LYRICS)
        it.reviewed = True
        self.assertEqual(it.status, batch.REVIEWED)
        it.rendered = True
        self.assertEqual(it.status, batch.DONE)
        it.busy = batch.RENDERING
        self.assertEqual(it.status, batch.RENDERING)
        it.busy, it.error = "", "deu ruim"
        self.assertEqual(it.status, batch.ERROR)

    def test_new_lyrics_reset_review_and_video(self):
        it = BatchItem("a.mp3", "A", strophes=[{}], reviewed=True, rendered=True, error="x")
        it.set_strophes([{"id": 2}])
        self.assertEqual((it.reviewed, it.rendered, it.error), (False, False, ""))

    def test_every_status_has_a_label(self):
        for st in (batch.PENDING, batch.LYRICS, batch.REVIEWED, batch.DONE, batch.ERROR,
                   batch.GENERATING, batch.RENDERING):
            self.assertIn(st, batch.STATUS_LABELS)


class LyricsFileTest(TmpDir):
    def test_same_name_txt(self):
        audio = self.file("onde_eu_fui.mp3")
        self.file("onde_eu_fui.txt", "[estrofe]\nAinda tenho a foto\n")
        self.assertEqual(batch.find_lyrics_file(audio), "[estrofe]\nAinda tenho a foto")

    def test_case_insensitive_and_windows_encoding(self):
        audio = self.file("Mar_Azul.mp3")
        self.file("mar_azul.TXT", "Coração", encoding="cp1252")
        self.assertEqual(batch.find_lyrics_file(audio), "Coração")

    def test_no_txt(self):
        self.assertEqual(batch.find_lyrics_file(self.file("x.mp3")), "")


class NewItemsTest(TmpDir):
    def test_titles_lyrics_and_duplicates(self):
        a = self.file("onde_eu_fui.mp3")
        b = self.file("sorriso_solto.mp3")
        self.file("sorriso_solto.txt", "Sem olhar pra trás")
        items = batch.new_items([a, b], title)
        self.assertEqual([(i.title, i.lyrics) for i in items],
                         [("Onde Eu Fui", ""), ("Sorriso Solto", "Sem olhar pra trás")])
        again = batch.new_items([a, b, a], title, existing=items)
        self.assertEqual(again, [])

    def test_output_path(self):
        it = BatchItem("a.mp3", "Onde: Eu Fui")
        safe = lambda t: t.replace(":", "")
        self.assertEqual(batch.output_path(it, "C:/videos", safe), os.path.join("C:/videos", "Onde Eu Fui.mp4"))
        it.output_dir = "D:/outra"
        self.assertEqual(batch.output_path(it, "C:/videos", safe), os.path.join("D:/outra", "Onde Eu Fui.mp4"))


class PersistenceTest(TmpDir):
    def test_round_trip_without_transient_state(self):
        path = os.path.join(self.dir, "sub", "lote.json")
        items = [BatchItem("a.mp3", "A", lyrics="la", strophes=[{"id": 1, "text": "x"}],
                           reviewed=True, busy=batch.RENDERING)]
        batch.save_batch(path, items, "C:/videos")
        loaded, folder = batch.load_batch(path)
        self.assertEqual(folder, "C:/videos")
        self.assertEqual(loaded[0].busy, "")  # not resumed as "running"
        items[0].busy = ""
        self.assertEqual(loaded, items)
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("busy", json.load(f)["items"][0])

    def test_missing_or_broken_file(self):
        self.assertEqual(batch.load_batch(os.path.join(self.dir, "nada.json")), ([], ""))
        bad = self.file("lote.json", "{nope")
        self.assertEqual(batch.load_batch(bad), ([], ""))
        weird = self.file("lote2.json", json.dumps({"items": [1, {"audio_file": "a", "title": "A",
                                                                  "futuro": True}]}))
        items, _ = batch.load_batch(weird)
        self.assertEqual([i.title for i in items], ["A"])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.items = [BatchItem(f"{n}.mp3", n.title()) for n in ("um", "dois", "tres")]
        self.events = []
        self.calls = []

    def runner(self, generate=None, render=None):
        def gen(item, progress, cancel):
            self.calls.append(("gen", item.title))
            progress(0.5)
            return [{"id": 1, "start_time": 0, "end_time": 1, "text": item.title}]

        def ren(item, out, progress, cancel):
            self.calls.append(("render", item.title))
            return True, out

        return BatchRunner(self.items, generate or gen, render or ren,
                           out_path=lambda it: f"C:/v/{it.title}.mp4",
                           on_event=lambda k, i, v: self.events.append((k, i, v)))

    def test_lyrics_only(self):
        summary = self.runner().run([0, 1, 2], do_lyrics=True, do_render=False)
        self.assertEqual(summary, {"lyrics": 3, "videos": 0, "errors": 0, "cancelled": False})
        self.assertEqual([i.status for i in self.items], [batch.LYRICS] * 3)
        self.assertIn(("start", 0, batch.GENERATING), self.events)
        self.assertIn(("progress", 1, 0.5), self.events)
        self.assertEqual(self.events[-1][0], "finished")

    def test_songs_with_lyrics_are_not_regenerated(self):
        self.items[1].set_strophes([{"id": 9}])
        self.runner().run([0, 1, 2], do_lyrics=True, do_render=False)
        self.assertEqual([c[1] for c in self.calls], ["Um", "Tres"])
        self.assertEqual(self.items[1].strophes, [{"id": 9}])

    def test_direct_does_lyrics_then_video_song_by_song(self):
        self.runner().run([0, 1], do_lyrics=True, do_render=True)
        self.assertEqual(self.calls, [("gen", "Um"), ("render", "Um"), ("gen", "Dois"), ("render", "Dois")])
        self.assertEqual(self.items[0].output_file, "C:/v/Um.mp4")
        self.assertEqual(self.items[0].status, batch.DONE)
        self.assertEqual(self.items[2].status, batch.PENDING)  # not in the queue

    def test_render_skips_songs_without_lyrics_and_skip_list(self):
        for it in self.items:
            it.set_strophes([{"id": 1}])
        self.items[0].strophes = []
        self.runner().run([0, 1, 2], do_lyrics=False, do_render=True, skip_render=[2])
        self.assertEqual(self.calls, [("render", "Dois")])

    def test_an_error_does_not_stop_the_batch(self):
        def gen(item, progress, cancel):
            if item.title == "Dois":
                raise RuntimeError("áudio estragado")
            return [{"id": 1}]
        summary = self.runner(generate=gen).run([0, 1, 2], do_lyrics=True, do_render=False)
        self.assertEqual((summary["lyrics"], summary["errors"]), (2, 1))
        self.assertEqual(self.items[1].status, batch.ERROR)
        self.assertEqual(self.items[1].error, "áudio estragado")

    def test_render_failure_is_an_error(self):
        for it in self.items:
            it.set_strophes([{"id": 1}])
        summary = self.runner(render=lambda *a: (False, "FFmpeg erro")).run([0], False, True)
        self.assertEqual((summary["errors"], self.items[0].error), (1, "FFmpeg erro"))

    def test_empty_lyrics_result_is_an_error(self):
        self.runner(generate=lambda *a: []).run([0], True, False)
        self.assertEqual(self.items[0].status, batch.ERROR)

    def test_cancel_stops_after_the_current_step(self):
        runner = None

        def gen(item, progress, cancel):
            self.calls.append(item.title)
            runner.cancel()          # user clicks ⏹ while the first song is running
            raise batch.Cancelled()
        runner = self.runner(generate=gen)
        summary = runner.run([0, 1, 2], True, False)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(self.calls, ["Um"])
        self.assertEqual([i.status for i in self.items], [batch.PENDING] * 3)  # nothing half-done

    def test_cancelled_render_is_not_marked_done(self):
        for it in self.items:
            it.set_strophes([{"id": 1}])
        runner = None

        def ren(item, out, progress, cancel):
            runner.cancel()
            return False, "Renderização cancelada."
        runner = self.runner(render=ren)
        runner.run([0, 1], False, True)
        self.assertFalse(self.items[0].rendered)
        self.assertEqual(self.items[0].error, "")

    def test_runs_in_a_thread(self):
        t = threading.Thread(target=self.runner().run, args=([0, 1, 2], True, True))
        t.start()
        t.join(5)
        self.assertEqual([i.status for i in self.items], [batch.DONE] * 3)


if __name__ == "__main__":
    unittest.main()
