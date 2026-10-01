"""FrameRenderer (frames in memory) and the ffmpeg command builder."""
import os
import tempfile
import unittest

from PIL import Image

import lyric_renderer as lr


def small_project(**kw):
    kw.setdefault("video_width", 320)
    kw.setdefault("video_height", 180)
    kw.setdefault("fps", 10)
    kw.setdefault("title_size", 20)
    kw.setdefault("lyric_size", 20)
    kw.setdefault("strophes", [lr.Strophe(1, 1.0, 3.0, "Olá\nmundo")])
    return lr.Project(**kw)


class AlphaTest(unittest.TestCase):
    def setUp(self):
        self.r = lr.FrameRenderer(small_project(fade_duration=0.5))

    def test_outside_is_zero(self):
        self.assertEqual(self.r._alpha(0.9, 1, 3), 0)
        self.assertEqual(self.r._alpha(1.0, 1, 3), 0)
        self.assertEqual(self.r._alpha(3.0, 1, 3), 0)

    def test_fades_and_plateau(self):
        self.assertEqual(self.r._alpha(2.0, 1, 3), 1.0)
        self.assertAlmostEqual(self.r._alpha(1.25, 1, 3), 0.5)  # smoothstep midpoint
        self.assertAlmostEqual(self.r._alpha(2.75, 1, 3), 0.5)
        self.assertLess(self.r._alpha(1.1, 1, 3), self.r._alpha(1.4, 1, 3))

    def test_fade_capped_at_30_percent_of_short_segments(self):
        # 1s segment: fade becomes 0.3s even though setting is 0.5s
        self.assertEqual(self.r._alpha(1.3, 1, 2), 1.0)

    def test_zero_fade(self):
        r = lr.FrameRenderer(small_project(fade_duration=0))
        self.assertEqual(r._alpha(1.001, 1, 3), 1.0)


class ActiveStropheTest(unittest.TestCase):
    def test_back_to_back_boundary_shows_next(self):
        r = lr.FrameRenderer(small_project(strophes=[
            lr.Strophe(2, 5, 10, "b"), lr.Strophe(1, 0, 5, "a")]))
        self.assertEqual(r.active_strophe(2).text, "a")
        self.assertIsNone(r.active_strophe(5))  # boundary: both at alpha 0
        self.assertEqual(r.active_strophe(5.04).text, "b")
        self.assertIsNone(r.active_strophe(11))

    def test_lyrics_end_is_max_end_not_last_start(self):
        r = lr.FrameRenderer(small_project(strophes=[
            lr.Strophe(1, 0, 20, "longa"), lr.Strophe(2, 5, 8, "curta")]))
        self.assertEqual(r.lyrics_end, 20)


class RenderFrameTest(unittest.TestCase):
    def test_transparent_frame(self):
        r = lr.FrameRenderer(small_project(transparent_bg=True))
        empty = r.render_frame(0.5)
        self.assertEqual(empty.mode, "RGBA")
        self.assertEqual(empty.getextrema()[3], (0, 0))  # fully transparent
        full = r.render_frame(2.0)
        self.assertEqual(full.getextrema()[3][1], 255)  # text is opaque
        self.assertEqual(len(r.render_frame_bytes(2.0)), 320 * 180 * 4)

    def test_solid_background(self):
        r = lr.FrameRenderer(small_project(transparent_bg=False, bg_color="#102030"))
        img = r.render_frame(0.5)
        self.assertEqual(img.getpixel((0, 0)), (16, 32, 48, 255))
        self.assertEqual(len(r.render_frame_bytes(0.5)), 320 * 180 * 3)

    def test_background_image_disables_transparency(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bg.png")
            Image.new("RGB", (64, 64), (200, 0, 0)).save(path)
            r = lr.FrameRenderer(small_project(transparent_bg=True, bg_image=path))
            self.assertFalse(r._transparent)
            self.assertEqual(r.render_frame(0.5).getpixel((5, 5)), (200, 0, 0, 255))

    def test_render_without_strophes(self):
        r = lr.FrameRenderer(small_project(strophes=[]))
        self.assertEqual(r.render_frame(1).getextrema()[3], (0, 0))
        ok, msg = r.render_video("x.mp4")
        self.assertFalse(ok)


class BuildFfmpegCmdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = os.path.join(self.tmp.name, "song.mp3")
        open(self.audio, "wb").close()

    def tearDown(self):
        self.tmp.cleanup()

    def _opt(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_transparent_forces_webm_with_alpha(self):
        cmd, out = lr.build_ffmpeg_cmd("C:/v/song.mp4", 1920, 1080, 25, True)
        self.assertTrue(out.endswith("song.webm"))
        self.assertEqual(cmd[-1], out)
        self.assertEqual(self._opt(cmd, "-c:v"), "libvpx-vp9")
        self.assertIn("yuva420p", cmd)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "rgba")

    def test_transparent_webm_with_audio_uses_opus(self):
        # AAC is not allowed in WebM — this used to make every render fail
        cmd, _ = lr.build_ffmpeg_cmd("song.webm", 1920, 1080, 25, True, self.audio)
        self.assertEqual(self._opt(cmd, "-c:a"), "libopus")
        self.assertIn("-shortest", cmd)

    def test_opaque_webm_uses_vp9_without_alpha(self):
        cmd, out = lr.build_ffmpeg_cmd("song.webm", 1280, 720, 30, False, self.audio)
        self.assertEqual(out, "song.webm")
        self.assertEqual(self._opt(cmd, "-c:v"), "libvpx-vp9")
        self.assertNotIn("yuva420p", cmd)
        self.assertEqual(self._opt(cmd, "-c:a"), "libopus")

    def test_mp4(self):
        cmd, out = lr.build_ffmpeg_cmd("song.mp4", 1280, 720, 30, False, self.audio)
        self.assertEqual(out, "song.mp4")
        self.assertEqual(self._opt(cmd, "-c:v"), "libx264")
        self.assertEqual(self._opt(cmd, "-c:a"), "aac")
        self.assertEqual(self._opt(cmd, "-s"), "1280x720")
        self.assertEqual(self._opt(cmd, "-r"), "30")

    def test_missing_audio_is_ignored(self):
        cmd, _ = lr.build_ffmpeg_cmd("song.mp4", 1280, 720, 30, False, "nao_existe.mp3")
        self.assertNotIn("-c:a", cmd)
        self.assertEqual(cmd.count("-i"), 1)


if __name__ == "__main__":
    unittest.main()
