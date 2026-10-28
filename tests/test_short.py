"""Short mode (9:16): screens, word timing, effects, background crop, ffmpeg excerpt."""
import json
import os
import tempfile
import unittest

from PIL import Image

import lyric_renderer as lr

VERSE = ("Sem olhar pra trás, sem medo algum\nA felicidade mora no comum\n"
         "Foi tanta coisa que eu vivi\nE agora sei que estou aqui")


def timed_words(text, start, step=0.4, gap_after_line=None):
    """[word, start, end] for each word, back to back; optional pause after line 2."""
    out, t = [], start
    for i, line in enumerate(text.split("\n")):
        for w in line.split():
            out.append([w, t, t + step])
            t += step
        if gap_after_line is not None and i == gap_after_line:
            t += 2.0
    return out


def short_project(**kw):
    p = lr.Project(title="Sorriso Solto", mode="short", **kw)
    p.short.clip_start, p.short.clip_end = 0.0, 20.0
    return p


class WordTimingTest(unittest.TestCase):
    def test_estimate_spreads_by_length(self):
        words = lr.estimate_words(["a", "palavra"], 0, 10)
        self.assertEqual([w[0] for w in words], ["a", "palavra"])
        self.assertLess(words[0][2] - words[0][1], words[1][2] - words[1][1])
        self.assertGreaterEqual(words[0][1], 0)
        self.assertLessEqual(words[-1][2], 10)

    def test_real_timing_is_used_when_it_matches_the_text(self):
        s = lr.Strophe(1, 0, 5, "oi tudo", words=[["oi", 1.0, 1.5], ["tudo", 2.0, 2.5]])
        self.assertEqual(lr.strophe_words(s), [("oi", 1.0, 1.5), ("tudo", 2.0, 2.5)])

    def test_text_edited_falls_back_to_estimate(self):
        s = lr.Strophe(1, 0, 5, "oi tudo bem", words=[["oi", 1.0, 1.5], ["tudo", 2.0, 2.5]])
        self.assertEqual(len(lr.strophe_words(s)), 3)

    def test_rescale_when_times_are_edited(self):
        w = lr.rescale_words([["a", 10, 12], ["b", 12, 14]], (10, 14), (20, 28))
        self.assertEqual(w, [["a", 20, 24], ["b", 24, 28]])
        self.assertEqual(lr.rescale_words([], (0, 1), (0, 2)), [])

    def test_merge_keeps_word_timing(self):
        a = lr.Strophe(1, 0, 2, "oi", words=[["oi", 0.5, 1.0]])
        b = lr.Strophe(2, 3, 5, "tchau")
        merged = lr.merge_with_next([a, b], 1)[0]
        self.assertEqual(merged.words[0], ["oi", 0.5, 1.0])
        self.assertEqual(len(merged.words), 2)
        # nothing known: stays empty (estimated at render time)
        plain = lr.merge_with_next([lr.Strophe(1, 0, 2, "x"), lr.Strophe(2, 3, 5, "y")], 1)[0]
        self.assertEqual(plain.words, [])


class BuildScreensTest(unittest.TestCase):
    def test_normal_video_is_one_screen_per_strophe(self):
        s = lr.Strophe(1, 0, 10, VERSE)
        screens = lr.build_screens([s])
        self.assertEqual(len(screens), 1)
        self.assertEqual((screens[0].start, screens[0].end), (0, 10))
        self.assertEqual(screens[0].text, VERSE)

    def test_two_lines_per_screen_with_pause(self):
        s = lr.Strophe(1, 0.7, 10, VERSE, words=timed_words(VERSE, 1.0, gap_after_line=1))
        a, b = lr.build_screens([s], 2, fade=0.35)
        self.assertEqual(a.text, VERSE.split("\n")[0] + "\n" + VERSE.split("\n")[1])
        last_word_end = a.lines[-1][-1][2]
        first_word_next = b.lines[0][0][1]
        # room for the fades: ends after the last word, next starts before its first
        self.assertAlmostEqual(a.end, last_word_end + 0.35)
        self.assertAlmostEqual(b.start, first_word_next - 0.35)
        self.assertEqual((a.start, b.end), (0.7, 10))

    def test_back_to_back_lines_switch_in_the_middle(self):
        s = lr.Strophe(1, 0.7, 10, VERSE, words=timed_words(VERSE, 1.0))
        a, b = lr.build_screens([s], 2, fade=0.35)
        self.assertLess(a.end, b.start)
        self.assertAlmostEqual((a.end + b.start) / 2, a.lines[-1][-1][2], places=2)

    def test_one_line_per_screen(self):
        s = lr.Strophe(1, 0, 10, VERSE)  # no word timing: estimated
        screens = lr.build_screens([s], 1)
        self.assertEqual([sc.text for sc in screens], VERSE.split("\n"))
        for x, y in zip(screens, screens[1:]):
            self.assertLessEqual(x.end, y.start)
        self.assertEqual((screens[0].start, screens[-1].end), (0, 10))


class CoverBoxTest(unittest.TestCase):
    def test_landscape_into_portrait(self):
        l, t, r, b = lr.cover_box(1920, 1080, 1080, 1920)
        self.assertAlmostEqual(b - t, 1080)               # full height
        self.assertAlmostEqual((r - l) / (b - t), 1080 / 1920)  # 9:16, no stretching
        self.assertAlmostEqual(l, (1920 - (r - l)) / 2)   # centered

    def test_position_and_zoom(self):
        left_edge = lr.cover_box(1920, 1080, 1080, 1920, pos_x=0.0)
        right_edge = lr.cover_box(1920, 1080, 1080, 1920, pos_x=1.0)
        self.assertEqual(left_edge[0], 0)
        self.assertAlmostEqual(right_edge[2], 1920)
        zoomed = lr.cover_box(1920, 1080, 1080, 1920, zoom=2.0)
        self.assertAlmostEqual(zoomed[3] - zoomed[1], 540)


class WrapTest(unittest.TestCase):
    def test_balanced(self):
        font = lr.find_font("mvboli", 92)
        words = [(w, 0, 0) for w in "A felicidade mora no comum".split()]
        lines = lr.wrap_words(words, font, 900)
        self.assertEqual([" ".join(w[0] for w in l) for l in lines], ["A felicidade", "mora no comum"])

    def test_fitting_line_is_untouched(self):
        font = lr.find_font("mvboli", 40)
        words = [(w, 0, 0) for w in "curta".split()]
        self.assertEqual(lr.wrap_words(words, font, 900), [words])


class ShortRendererTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bg = os.path.join(self.tmp.name, "bg.png")
        img = Image.new("RGB", (1920, 1080), (0, 0, 255))
        img.paste((255, 0, 0), (0, 0, 960, 1080))  # left half red, right half blue
        img.save(self.bg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_vertical_size_and_never_transparent(self):
        r = lr.FrameRenderer(short_project(transparent_bg=True))
        self.assertEqual((r.w, r.h), (1080, 1920))
        self.assertFalse(r._transparent)
        self.assertFalse(lr.project_is_transparent(short_project(transparent_bg=True)))

    def test_background_position(self):
        p = short_project(bg_image=self.bg)
        p.short.fx_zoom = False
        p.short.bg_x = 0.0
        self.assertEqual(lr.FrameRenderer(p).render_frame(0.1).getpixel((540, 1900))[:3], (255, 0, 0))
        p.short.bg_x = 1.0
        self.assertEqual(lr.FrameRenderer(p).render_frame(0.1).getpixel((540, 1900))[:3], (0, 0, 255))

    def test_zoom_effect_moves_the_background(self):
        p = short_project(bg_image=self.bg)
        p.short.bg_x = 0.5
        r = lr.FrameRenderer(p)
        # the red/blue edge is in the middle; zooming in pushes it away from x=540±
        start = r.render_frame(0.01).convert("RGB")
        end = r.render_frame(19.99).convert("RGB")
        self.assertNotEqual(start.tobytes(), end.tobytes())
        p.short.fx_zoom = False
        still = lr.FrameRenderer(p)
        self.assertEqual(still.render_frame(0.01).tobytes(), still.render_frame(19.99).tobytes())

    def _text_box(self, img):
        """Bounding box of white-ish or karaoke-yellow text pixels on a black bg."""
        return img.convert("L").point(lambda v: 255 if v > 60 else 0).getbbox()

    def _lyrics_only(self, **fx):
        p = short_project(bg_color="#000000", transparent_bg=False)
        p.title = ""
        for k in ("fx_slide", "fx_transition", "fx_zoom", "fx_karaoke"):
            setattr(p.short, k, fx.get(k, False))
        p.short.fade_duration = 1.0
        p.strophes = [lr.Strophe(1, 2, 10, "olá mundo")]
        return lr.FrameRenderer(p)

    def test_slide_effect(self):
        plain, slide = self._lyrics_only(), self._lyrics_only(fx_slide=True)
        mid = self._text_box(plain.render_frame(6))
        self.assertEqual(self._text_box(slide.render_frame(6)), mid)        # still in the middle
        entering = self._text_box(slide.render_frame(2.5))
        leaving = self._text_box(slide.render_frame(9.6))
        self.assertLess(entering[0], mid[0])   # comes from the left
        self.assertGreater(leaving[0], mid[0])  # goes to the right

    def test_transition_effect(self):
        plain, rise = self._lyrics_only(), self._lyrics_only(fx_transition=True)
        mid = self._text_box(plain.render_frame(6))
        self.assertGreater(self._text_box(rise.render_frame(2.5))[1], mid[1])  # comes from below
        self.assertLess(self._text_box(rise.render_frame(9.6))[1], mid[1])     # leaves upwards

    def test_karaoke_colors_sung_words(self):
        r = self._lyrics_only(fx_karaoke=True)
        r.screens[0].lines[0][:] = [("olá", 3.0, 4.0), ("mundo", 7.0, 8.0)]

        def yellow_pixels(t):
            img = r.render_frame(t).convert("RGB")
            return sum(1 for px in img.getdata() if px[0] > 200 and px[1] > 150 and px[2] < 120)
        self.assertEqual(yellow_pixels(2.95), 0)  # nothing sung yet
        some, more = yellow_pixels(5), yellow_pixels(7.5)
        self.assertGreater(some, 0)
        self.assertGreater(more, some)

    def test_title_shows_during_the_whole_clip(self):
        p = short_project(bg_color="#000000", transparent_bg=False)
        p.short.clip_start, p.short.clip_end = 30, 50
        p.strophes = [lr.Strophe(1, 40, 45, "x")]
        r = lr.FrameRenderer(p)
        top = lambda t: r.render_frame(t).convert("L").crop((0, 0, 1080, 400)).getbbox()
        self.assertIsNotNone(top(31))   # before the first strophe of the clip
        self.assertIsNone(top(29))      # outside the clip

    def test_preview_is_vertical(self):
        img = lr.render_preview(short_project(bg_image=self.bg), "olá\nmundo", 144, 256)
        self.assertEqual(img.size, (144, 256))

    def test_normal_video_frames_are_unchanged_by_short_settings(self):
        p = lr.Project(strophes=[lr.Strophe(1, 1, 5, "olá")], transparent_bg=False)
        before = lr.FrameRenderer(p).render_frame(3).tobytes()
        p.short.fx_slide = p.short.fx_karaoke = True
        p.short.lyric_size = 150
        self.assertEqual(lr.FrameRenderer(p).render_frame(3).tobytes(), before)


class ShortProjectFileTest(unittest.TestCase):
    def test_round_trip(self):
        p = short_project(strophes=[lr.Strophe(1, 0, 2, "a b", words=[["a", 0.1, 0.5], ["b", 0.6, 1.0]])])
        p.short.bg_x, p.short.lines_per_screen = 0.2, 1
        q = lr.Project.from_dict(json.loads(json.dumps(p.to_dict())))
        self.assertEqual(q, p)
        self.assertIsInstance(q.short, lr.ShortSettings)

    def test_old_files_open(self):
        q = lr.Project.from_dict({"title": "Velho", "strophes": [
            {"id": 1, "start_time": 0, "end_time": 1, "text": "a"}]})
        self.assertEqual((q.mode, q.short, q.strophes[0].words), ("video", lr.ShortSettings(), []))

    def test_short_style_is_remembered(self):
        p = lr.Project()
        p.short.lyric_size, p.short.fx_karaoke, p.short.bg_x = 120, False, 0.3
        p.short.clip_start = 50  # per song: not part of the style
        q = lr.Project()
        lr.apply_style(q, json.loads(json.dumps(lr.style_of(p))))
        self.assertEqual((q.short.lyric_size, q.short.fx_karaoke, q.short.bg_x), (120, False, 0.3))
        self.assertEqual(q.short.clip_start, 0.0)


class FfmpegExcerptCmdTest(unittest.TestCase):
    def test_excerpt_with_fades(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            audio = f.name
        try:
            cmd, _ = lr.build_ffmpeg_cmd("s.mp4", 1080, 1920, 25, False, audio,
                                         audio_start=62.5, audio_duration=30)
        finally:
            os.remove(audio)
        i = cmd.index(audio)
        self.assertEqual(cmd[i - 5:i], ["-ss", "62.500", "-t", "30.000", "-i"])
        af = cmd[cmd.index("-af") + 1]
        self.assertIn("afade=t=in:st=0", af)
        self.assertIn("afade=t=out:st=29.000:d=1.000", af)
        self.assertEqual(cmd[cmd.index("-s") + 1], "1080x1920")

    def test_full_song_has_no_excerpt_args(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            audio = f.name
        try:
            cmd, _ = lr.build_ffmpeg_cmd("v.mp4", 1920, 1080, 25, False, audio)
        finally:
            os.remove(audio)
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("-af", cmd)


if __name__ == "__main__":
    unittest.main()
