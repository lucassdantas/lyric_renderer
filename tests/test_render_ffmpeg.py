"""End-to-end renders through the real ffmpeg. Skipped when ffmpeg isn't installed."""
import os
import shutil
import subprocess
import tempfile
import unittest

import lyric_renderer as lr

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def ffprobe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries,
         "-of", "default=noprint_wrappers=1", path],
        capture_output=True, text=True).stdout


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe não encontrados")
class FfmpegRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.audio = os.path.join(cls.tmp.name, "tone.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=5", cls.audio], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def project(self, **kw):
        return lr.Project(video_width=160, video_height=90, fps=10,
                          title_size=12, lyric_size=12,
                          strophes=[lr.Strophe(1, 0.5, 2.0, "oi")], **kw)

    def out(self, name):
        return os.path.join(self.tmp.name, name)

    def test_transparent_webm_with_audio(self):
        ok, path = lr.FrameRenderer(self.project(transparent_bg=True)).render_video(
            self.out("t.mp4"), audio_path=self.audio)
        self.assertTrue(ok, path)
        self.assertTrue(path.endswith(".webm"))
        info = ffprobe(path, "stream=codec_name:stream_tags=alpha_mode")
        self.assertIn("codec_name=vp9", info)
        self.assertIn("codec_name=opus", info)
        self.assertIn("alpha_mode=1", info.lower())

    def test_mp4_keeps_full_song(self):
        # Lyrics end at 2s but the song is 5s: the video must not cut the song
        ok, path = lr.FrameRenderer(self.project(transparent_bg=False)).render_video(
            self.out("a.mp4"), audio_path=self.audio)
        self.assertTrue(ok, path)
        self.assertGreaterEqual(lr.probe_duration(path), 4.8)

    def test_duration_uses_latest_ending_strophe(self):
        p = self.project(transparent_bg=False)
        p.strophes = [lr.Strophe(1, 0, 4, "longa"), lr.Strophe(2, 1, 2, "curta")]
        ok, path = lr.FrameRenderer(p).render_video(self.out("d.mp4"))
        self.assertTrue(ok, path)
        self.assertGreaterEqual(lr.probe_duration(path), 5.4)  # 4 + 1.5 tail

    def test_cancel(self):
        ok, msg = lr.FrameRenderer(self.project()).render_video(
            self.out("c.webm"), cancel_flag=lambda: True)
        self.assertFalse(ok)
        self.assertIn("cancelada", msg)

    def test_progress_reaches_100(self):
        seen = []
        ok, _ = lr.FrameRenderer(self.project(transparent_bg=False)).render_video(
            self.out("p.mp4"), progress_callback=seen.append)
        self.assertTrue(ok)
        self.assertEqual(seen[-1], 1.0)


class MissingFfmpegTest(unittest.TestCase):
    def test_reports_error_instead_of_crashing(self):
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            p = lr.Project(video_width=64, video_height=36, fps=5,
                           strophes=[lr.Strophe(1, 0, 1, "x")])
            ok, msg = lr.FrameRenderer(p).render_video(
                os.path.join(tempfile.gettempdir(), "never.mp4"))
        finally:
            os.environ["PATH"] = old
        self.assertFalse(ok)
        self.assertIn("FFmpeg", msg)


if __name__ == "__main__":
    unittest.main()
