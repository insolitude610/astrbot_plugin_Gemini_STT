"""单元测试：audio_source 模块（音频来源解析，无真实网络）"""

import asyncio
import base64
import os
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import audio_source
import security
from settings import PluginSettings


def make_settings(**overrides) -> PluginSettings:
    cfg = {
        "path_remap_from": "",
        "path_remap_to": "",
        "enable_get_record_fallback": True,
    }
    cfg.update(overrides)
    return PluginSettings.from_config(cfg)


class TestExtractRecordFileToken(unittest.TestCase):
    def setUp(self):
        self.resolver = audio_source.AudioSourceResolver(make_settings())

    def test_attr_file(self):
        self.assertEqual(
            self.resolver.extract_record_file_token(SimpleNamespace(file="abc")), "abc"
        )

    def test_data_dict(self):
        self.assertEqual(
            self.resolver.extract_record_file_token(SimpleNamespace(data={"file": "xyz"})),
            "xyz",
        )

    def test_attr_order(self):
        self.assertEqual(
            self.resolver.extract_record_file_token(SimpleNamespace(path="p", url="u")),
            "p",
        )

    def test_empty(self):
        self.assertEqual(self.resolver.extract_record_file_token(SimpleNamespace()), "")


class TestStaticResolver(unittest.TestCase):
    def test_resolve_known_host(self):
        async def _t():
            resolver = audio_source.StaticResolver({"example.com": ["1.2.3.4"]})
            try:
                results = await resolver.resolve("example.com")
                self.assertEqual(len(results), 1)
                r = results[0]
                self.assertEqual(r["host"], "1.2.3.4")
                self.assertEqual(r["hostname"], "example.com")
                self.assertEqual(r["port"], 0)
                self.assertEqual(r["family"], socket.AF_INET)
            finally:
                await resolver.close()

        asyncio.run(_t())

    def test_resolve_unknown_host_raises_oserror(self):
        async def _t():
            resolver = audio_source.StaticResolver({"example.com": ["1.2.3.4"]})
            with self.assertRaises(OSError):
                await resolver.resolve("unknown")
            await resolver.close()

        asyncio.run(_t())

    def test_close_no_error(self):
        async def _t():
            resolver = audio_source.StaticResolver({"example.com": ["1.2.3.4"]})
            await resolver.close()
            await resolver.close()

        asyncio.run(_t())


class TestGetRecordFallbackPath(unittest.TestCase):
    def _resolver(self, **overrides) -> audio_source.AudioSourceResolver:
        return audio_source.AudioSourceResolver(make_settings(**overrides))

    @staticmethod
    def _event(api_mock) -> SimpleNamespace:
        return SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            bot=SimpleNamespace(api=SimpleNamespace(call_action=api_mock)),
        )

    def test_windows_data_dict(self):
        fd, tmp = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            api = mock.AsyncMock(return_value={"data": {"file": tmp}})
            resolver = self._resolver()
            result = asyncio.run(
                resolver.get_record_fallback_path(
                    self._event(api), SimpleNamespace(file="abc")
                )
            )
            self.assertEqual(result, os.path.realpath(os.path.abspath(tmp)))
            api.assert_awaited_once_with("get_record", file="abc", out_format="mp3")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def test_linux_flat_structure(self):
        fd, tmp = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            api = mock.AsyncMock(return_value={"file": tmp})
            resolver = self._resolver()
            result = asyncio.run(
                resolver.get_record_fallback_path(
                    self._event(api), SimpleNamespace(file="abc")
                )
            )
            self.assertEqual(result, os.path.realpath(os.path.abspath(tmp)))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def test_base64_payload_decoded_to_temp_file(self):
        payload = b"audio-bytes"
        api = mock.AsyncMock(
            return_value={"base64": base64.b64encode(payload).decode()}
        )
        resolver = self._resolver()
        result = asyncio.run(
            resolver.get_record_fallback_path(self._event(api), SimpleNamespace(file="abc"))
        )
        self.assertTrue(result)
        self.assertTrue(os.path.isfile(result))
        try:
            with open(result, "rb") as f:
                self.assertEqual(f.read(), payload)
        finally:
            if os.path.exists(result):
                os.remove(result)

    def test_http_loopback_url_calls_trusted_download(self):
        fd, tmp = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            api = mock.AsyncMock(return_value={"file": "http://127.0.0.1:3000/a.mp3"})
            resolver = self._resolver(
                allow_napcat_local_record_url=True, block_private_network=True
            )
            with mock.patch.object(
                resolver,
                "download_trusted_record_url",
                new=mock.AsyncMock(return_value=tmp),
            ) as m_dl:
                result = asyncio.run(
                    resolver.get_record_fallback_path(
                        self._event(api), SimpleNamespace(file="abc")
                    )
                )
                self.assertEqual(result, tmp)
                m_dl.assert_awaited_once_with("http://127.0.0.1:3000/a.mp3")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


class TestDownloadRemoteAudio(unittest.TestCase):
    def test_blocked_by_security_prepare(self):
        resolver = audio_source.AudioSourceResolver(make_settings())
        with mock.patch.object(
            security,
            "prepare_remote_target",
            new=mock.AsyncMock(return_value=(False, None)),
        ):
            result = asyncio.run(
                resolver.download_remote_audio("http://evil.example.com/a.mp3")
            )
        self.assertEqual(result, "")


class TestGetVoiceData(unittest.TestCase):
    def test_mp3_returns_b64_and_cleans_temp(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(tmpdir) if os.path.isdir(tmpdir) else None)
        mp3_path = os.path.join(tmpdir, "voice.mp3")
        payload = b"ID3\x04\x00\x00\x00fake-mp3-content"
        with open(mp3_path, "wb") as f:
            f.write(payload)

        resolver = audio_source.AudioSourceResolver(
            make_settings(local_audio_allowed_dirs=[tmpdir])
        )
        event = SimpleNamespace(
            path=mp3_path,
            get_platform_name=lambda: "aiocqhttp",
            bot=SimpleNamespace(api=mock.AsyncMock(return_value={})),
        )
        b64, mime = asyncio.run(
            resolver.get_voice_data(event, SimpleNamespace(path=mp3_path))
        )
        self.assertEqual(mime, "audio/mpeg")
        self.assertEqual(base64.b64decode(b64), payload)
        self.assertFalse(os.path.exists(mp3_path))


class TestResolveOriginalAudioPathWait(unittest.TestCase):
    def test_waits_for_local_file_to_appear(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(tmpdir) if os.path.isdir(tmpdir) else None)
        mp3_path = os.path.join(tmpdir, "late_voice.mp3")

        resolver = audio_source.AudioSourceResolver(
            make_settings(
                voice_file_wait_sec=2,
                local_audio_allowed_dirs=[tmpdir],
            )
        )
        event = SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            bot=SimpleNamespace(api=mock.AsyncMock(return_value={})),
        )

        def _create():
            time.sleep(1.0)
            with open(mp3_path, "wb") as f:
                f.write(b"ID3late")

        t = threading.Thread(target=_create)
        t.start()
        try:
            result = asyncio.run(
                resolver.resolve_original_audio_path(
                    event, SimpleNamespace(path=mp3_path)
                )
            )
            self.assertEqual(result, os.path.realpath(os.path.abspath(mp3_path)))
        finally:
            t.join(timeout=5)
            if os.path.exists(mp3_path):
                os.remove(mp3_path)


if __name__ == "__main__":
    unittest.main()
