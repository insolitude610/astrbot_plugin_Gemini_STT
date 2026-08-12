"""Tests for the audio conversion module (audio_convert.py)."""

import base64
import os
import tempfile
import unittest
from unittest import mock

import audio_convert


class TestDetectAudioFormat(unittest.TestCase):
    def setUp(self):
        self._tmp_files = []

    def _write(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self._tmp_files.append(path)
        return path

    def tearDown(self):
        for path in self._tmp_files:
            if os.path.exists(path):
                os.remove(path)

    def test_formats(self):
        cases = [
            (b"ID3\x04\x00\x00\x00\x00\x00\x00", "mp3"),
            (b"\xff\xfb\x90\x64", "mp3"),
            (b"#!AMR\n", "amr"),
            (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
            (b"#!SILK_V3", "silk"),
            (b"whatever", "unknown"),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    audio_convert.detect_audio_format(self._write(data)), expected
                )

    def test_missing_file_returns_unknown(self):
        self.assertEqual(audio_convert.detect_audio_format("no_such_file.bin"), "unknown")


class TestFindFfmpeg(unittest.TestCase):
    @staticmethod
    def _make_ffmpeg_file() -> str:
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        path = os.path.join(tempfile.mkdtemp(), name)
        with open(path, "wb") as f:
            f.write(b"")
        return path

    def test_custom_valid_path_returned_directly(self):
        path = self._make_ffmpeg_file()
        os.chmod(path, 0o777)
        with mock.patch.object(audio_convert.shutil, "which") as m_which:
            result = audio_convert.find_ffmpeg(path)
        self.assertEqual(result, path)
        m_which.assert_not_called()

    def test_custom_missing_falls_through_to_which(self):
        which_path = self._make_ffmpeg_file()
        logs = []
        with mock.patch.object(
            audio_convert.shutil, "which", return_value=which_path
        ) as m_which:
            result = audio_convert.find_ffmpeg(
                os.path.join(tempfile.gettempdir(), "gsv_no_such_ffmpeg.exe"),
                debug_log=logs.append,
            )
        self.assertEqual(result, which_path)
        m_which.assert_called_once()
        self.assertTrue(
            any("自定义ffmpeg_path不可执行或不存在" in msg for msg in logs)
        )

    def test_which_returns_real_file(self):
        which_path = self._make_ffmpeg_file()
        with mock.patch.object(
            audio_convert.shutil, "which", return_value=which_path
        ) as m_which:
            result = audio_convert.find_ffmpeg()
        self.assertEqual(result, which_path)
        m_which.assert_called_once()

    def test_fallback_subprocess_ok(self):
        expected = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        with mock.patch.object(audio_convert.shutil, "which", return_value=None), mock.patch.object(
            audio_convert.subprocess, "run"
        ) as m_run:
            m_run.return_value = mock.Mock(returncode=0)
            result = audio_convert.find_ffmpeg()
        self.assertEqual(result, expected)
        m_run.assert_called_once_with(
            [expected, "-version"], capture_output=True, timeout=6
        )

    def test_all_fail_returns_empty(self):
        with mock.patch.object(audio_convert.shutil, "which", return_value=None), mock.patch.object(
            audio_convert.subprocess, "run"
        ) as m_run:
            m_run.return_value = mock.Mock(returncode=1)
            self.assertEqual(audio_convert.find_ffmpeg(), "")


class TestConvertToMp3(unittest.TestCase):
    @staticmethod
    def _fake_run_ok(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"ID3fake")
        return mock.Mock(returncode=0, stderr=b"")

    def test_success_returns_mp3_path(self):
        with mock.patch.object(
            audio_convert.subprocess, "run", side_effect=self._fake_run_ok
        ) as m_run:
            result = audio_convert.convert_to_mp3("ffmpeg", "in.ogg")
        self.assertTrue(result.endswith(".mp3"))
        self.assertTrue(os.path.basename(result).startswith("gsv_"))
        self.assertGreater(os.path.getsize(result), 0)
        m_run.assert_called_once()
        self.assertEqual(m_run.call_args.args[0][0], "ffmpeg")

    def test_pcm_command_variant(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            with open(cmd[-1], "wb") as f:
                f.write(b"\x00\x01\x02")
            return mock.Mock(returncode=0, stderr=b"")

        with mock.patch.object(audio_convert.subprocess, "run", side_effect=fake_run):
            audio_convert.convert_to_mp3("ffmpeg", "in.pcm", input_format="pcm")
        self.assertIn("-f", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("-f") + 1], "s16le")
        self.assertIn("24000", seen["cmd"])

    def test_timeout_passed_through(self):
        with mock.patch.object(
            audio_convert.subprocess, "run", side_effect=self._fake_run_ok
        ) as m_run:
            audio_convert.convert_to_mp3("ffmpeg", "in.ogg", timeout=45)
        self.assertEqual(m_run.call_args.kwargs["timeout"], 45)

    def test_failure_returns_empty(self):
        with mock.patch.object(audio_convert.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=1, stderr=b"boom")
            result = audio_convert.convert_to_mp3("ffmpeg", "in.ogg")
        self.assertEqual(result, "")

    def test_empty_ffmpeg_path_returns_empty(self):
        with mock.patch.object(audio_convert.subprocess, "run") as m_run:
            self.assertEqual(audio_convert.convert_to_mp3("", "in.ogg"), "")
        m_run.assert_not_called()


class TestConvertSilkToPcm(unittest.TestCase):
    def test_decode_ok_returns_true(self):
        pcm_path = os.path.join(tempfile.mkdtemp(), "out.pcm")
        logs = []

        def fake_decode(silk, pcm, **kwargs):
            with open(pcm, "wb") as f:
                f.write(b"\x00\x01")

        with mock.patch.object(audio_convert, "PILK_AVAILABLE", True), mock.patch(
            "audio_convert.pilk", create=True
        ) as m_pilk:
            m_pilk.decode.side_effect = fake_decode
            result = audio_convert.convert_silk_to_pcm(
                "in.silk", pcm_path, debug_log=logs.append
            )
        self.assertTrue(result)
        m_pilk.decode.assert_called_once_with("in.silk", pcm_path)
        self.assertEqual(logs, [])

    def test_decode_exception_returns_false(self):
        logs = []
        with mock.patch.object(audio_convert, "PILK_AVAILABLE", True), mock.patch(
            "audio_convert.pilk", create=True
        ) as m_pilk:
            m_pilk.decode.side_effect = RuntimeError("boom")
            result = audio_convert.convert_silk_to_pcm(
                "in.silk", "out.pcm", debug_log=logs.append
            )
        self.assertFalse(result)
        self.assertTrue(any("SILK解码失败" in msg for msg in logs))

    def test_pilk_unavailable_returns_false(self):
        with mock.patch.object(audio_convert, "PILK_AVAILABLE", False):
            self.assertFalse(audio_convert.convert_silk_to_pcm("in.silk", "out.pcm"))


class TestEncodeMp3B64WithLimit(unittest.TestCase):
    @staticmethod
    def _make_mp3(data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def test_ok_returns_b64_and_mime(self):
        payload = b"\x00\x01\x02\x03\xff\xfe"
        path = self._make_mp3(payload)
        self.addCleanup(os.remove, path)
        b64, mime = audio_convert.encode_mp3_b64_with_limit(path, max_audio_mb=1)
        self.assertEqual(b64, base64.b64encode(payload).decode())
        self.assertEqual(mime, "audio/mpeg")

    def test_over_limit_returns_none(self):
        path = self._make_mp3(b"\x00\x01")
        self.addCleanup(os.remove, path)
        logs = []
        b64, mime = audio_convert.encode_mp3_b64_with_limit(
            path, max_audio_mb=0, debug_log=logs.append
        )
        self.assertIsNone(b64)
        self.assertIsNone(mime)
        self.assertTrue(any("MP3超大小限制" in msg for msg in logs))

    def test_missing_file_returns_none(self):
        b64, mime = audio_convert.encode_mp3_b64_with_limit(
            "no_such_file.mp3", max_audio_mb=1
        )
        self.assertIsNone(b64)
        self.assertIsNone(mime)


if __name__ == "__main__":
    unittest.main()
