import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import aiohttp

from settings import PluginSettings
from stt_client import (
    STTClient,
    _build_auth,
    _normalize_api_base,
    backoff_sec,
    normalize_model_name,
)

AUDIO_B64 = base64.b64encode(b"fake-audio-bytes").decode()
GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "你好"}]}}]}


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._body = json.dumps(payload or {}) if payload is not None else ""

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


def make_settings(**overrides):
    overrides.setdefault("api_url", "https://aihubmix.com/gemini")
    overrides.setdefault("api_key", "test-key")
    return PluginSettings(**overrides)


def make_client(session, **overrides):
    return STTClient(
        make_settings(**overrides),
        debug_log=lambda msg: None,
        session_provider=lambda: session,
    )


class TestNormalizeApiBase(unittest.TestCase):
    def test_strips_v1_chat_completions(self):
        self.assertEqual(
            _normalize_api_base("https://x.com/v1/chat/completions"),
            "https://x.com",
        )

    def test_strips_v1(self):
        self.assertEqual(_normalize_api_base("https://x.com/v1"), "https://x.com")

    def test_gemini_suffix_kept_by_default(self):
        self.assertEqual(
            _normalize_api_base("https://aihubmix.com/gemini"),
            "https://aihubmix.com/gemini",
        )

    def test_gemini_suffix_stripped_when_requested(self):
        self.assertEqual(
            _normalize_api_base(
                "https://aihubmix.com/gemini", strip_gemini_suffix=True
            ),
            "https://aihubmix.com",
        )

    def test_v1_after_gemini_suffix_strips_only_v1(self):
        self.assertEqual(
            _normalize_api_base("https://aihubmix.com/gemini/v1"),
            "https://aihubmix.com/gemini",
        )

    def test_trailing_slash_handled(self):
        self.assertEqual(_normalize_api_base("https://x.com/v1/"), "https://x.com")

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_api_base(""), "")


class TestBuildAuth(unittest.TestCase):
    def test_query_mode_in_url(self):
        self.assertEqual(
            _build_auth("query", "k", query_key_in_url=True), ({}, "key=k")
        )

    def test_query_mode_header_quirk(self):
        self.assertEqual(
            _build_auth("query", "k", query_key_in_url=False),
            ({"Authorization": "Bearer k"}, ""),
        )

    def test_x_api_key_mode(self):
        for flag in (True, False):
            self.assertEqual(
                _build_auth("x-api-key", "k", query_key_in_url=flag),
                ({"x-api-key": "k"}, ""),
            )

    def test_api_key_mode(self):
        for flag in (True, False):
            self.assertEqual(
                _build_auth("api-key", "k", query_key_in_url=flag),
                ({"api-key": "k"}, ""),
            )

    def test_bearer_default(self):
        for flag in (True, False):
            self.assertEqual(
                _build_auth("bearer", "k", query_key_in_url=flag),
                ({"Authorization": "Bearer k"}, ""),
            )


class TestBackoffSec(unittest.TestCase):
    def test_bounds_with_real_random(self):
        v = backoff_sec(0)
        self.assertGreaterEqual(v, 1.0)
        self.assertLess(v, 1.31)

    def test_exact_values_with_uniform_mocked(self):
        with patch("stt_client.random.uniform", return_value=0):
            self.assertEqual(backoff_sec(0), 1.0)
            self.assertEqual(backoff_sec(1), 2.0)
            self.assertEqual(backoff_sec(2), 4.0)
            self.assertEqual(backoff_sec(3), 8.0)
            self.assertEqual(backoff_sec(10), 8.0)


class TestNormalizeModelName(unittest.TestCase):
    def test_strips_models_prefix(self):
        self.assertEqual(
            normalize_model_name("models/gemini-2.0-flash"), "gemini-2.0-flash"
        )

    def test_takes_last_path_segment(self):
        self.assertEqual(normalize_model_name("openai/gpt-4o"), "gpt-4o")

    def test_strips_bracket_prefix(self):
        self.assertEqual(normalize_model_name("[v1.2] gemini-x"), "gemini-x")

    def test_empty_falls_back_to_default(self):
        self.assertEqual(normalize_model_name(""), "gemini-2.0-flash")


class TestBuildGeminiUrl(unittest.TestCase):
    def test_preserves_gemini_suffix(self):
        client = STTClient(make_settings())
        self.assertEqual(
            client.build_gemini_url("https://aihubmix.com/gemini", "gemini-2.0-flash"),
            "https://aihubmix.com/gemini/v1beta/models/gemini-2.0-flash:generateContent",
        )

    def test_strips_chat_completions_base(self):
        client = STTClient(make_settings())
        self.assertEqual(
            client.build_gemini_url("https://x.com/v1/chat/completions", "gemini-x"),
            "https://x.com/v1beta/models/gemini-x:generateContent",
        )


class TestBuildSttInstruction(unittest.TestCase):
    def test_simple_mode_has_no_numbering(self):
        client = STTClient(make_settings())
        inst = client.build_stt_instruction()
        self.assertIn("语音转写器", inst)
        self.assertNotIn("1)", inst)

    def test_rich_mode_numbered_one_to_five(self):
        client = STTClient(make_settings(output_mode="rich"))
        inst = client.build_stt_instruction()
        self.assertIn("1) 原话转写", inst)
        self.assertIn("2) 语言", inst)
        self.assertIn("3) 语气/情绪", inst)
        self.assertIn("4) 环境音", inst)
        self.assertIn("5) 大意总结", inst)
        self.assertNotIn("6)", inst)

    def test_custom_instruction_returned_verbatim(self):
        client = STTClient(make_settings(voice_instruction="自定义指令"))
        self.assertEqual(client.build_stt_instruction(), "自定义指令")


class TestCallGemini(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_candidates_text(self):
        session = FakeSession(FakeResponse(200, GEMINI_OK))
        client = make_client(session)
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_gemini(AUDIO_B64, "audio/mpeg", "补充文字")
        self.assertEqual(text, "你好")
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertEqual(
            url,
            "https://aihubmix.com/gemini/v1beta/models/gemini-2.0-flash:generateContent",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        parts = kwargs["json"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "audio/mpeg")
        self.assertEqual(parts[0]["inline_data"]["data"], AUDIO_B64)
        self.assertIn("用户同时发送文字：补充文字", parts[1]["text"])

    async def test_status_500_returns_empty_without_retry(self):
        logs = []
        session = FakeSession(FakeResponse(500, {"error": "boom"}))
        client = STTClient(
            make_settings(), debug_log=logs.append, session_provider=lambda: session
        )
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_gemini(AUDIO_B64, "audio/mpeg", "")
        self.assertEqual(text, "")
        self.assertEqual(len(session.posts), 1)
        self.assertTrue(any("Gemini失败: 500 - " in m for m in logs))

    async def test_429_twice_then_200_retries(self):
        session = FakeSession(
            FakeResponse(429, {"error": "slow"}),
            FakeResponse(429, {"error": "slow"}),
            FakeResponse(200, GEMINI_OK),
        )
        client = make_client(session)
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_gemini(AUDIO_B64, "audio/mpeg", "")
        self.assertEqual(text, "你好")
        self.assertEqual(len(session.posts), 3)

    async def test_429_exhausted_returns_empty(self):
        logs = []
        session = FakeSession(FakeResponse(429, {"error": "quota"}))
        client = STTClient(
            make_settings(retry_times=2),
            debug_log=logs.append,
            session_provider=lambda: session,
        )
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_gemini(AUDIO_B64, "audio/mpeg", "")
        self.assertEqual(text, "")
        self.assertEqual(len(session.posts), 3)
        self.assertTrue(any("Gemini 429，第1次重试，等待0.01s" in m for m in logs))
        self.assertTrue(any("Gemini 429，第2次重试，等待0.01s" in m for m in logs))
        self.assertTrue(any(m.startswith("Gemini失败: 429") for m in logs))

    async def test_query_mode_appends_key_param(self):
        session = FakeSession(FakeResponse(200, GEMINI_OK))
        client = make_client(session, api_key_header="query")
        with patch("stt_client.backoff_sec", return_value=0.01):
            await client.call_gemini(AUDIO_B64, "audio/mpeg", "")
        url, kwargs = session.posts[0]
        self.assertIn("?key=test-key", url)
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertIn("Content-Type", kwargs["headers"])

    async def test_query_mode_uses_ampersand_when_url_has_query(self):
        session = FakeSession(FakeResponse(200, GEMINI_OK))
        client = make_client(
            session, api_url="https://x.com/v1?foo=bar", api_key_header="query"
        )
        with patch("stt_client.backoff_sec", return_value=0.01):
            await client.call_gemini(AUDIO_B64, "audio/mpeg", "")
        url, _ = session.posts[0]
        self.assertIn("&key=test-key", url)

    async def test_x_api_key_header_mode(self):
        session = FakeSession(FakeResponse(200, GEMINI_OK))
        client = make_client(session, api_key_header="x-api-key")
        with patch("stt_client.backoff_sec", return_value=0.01):
            await client.call_gemini(AUDIO_B64, "audio/mpeg", "")
        _, kwargs = session.posts[0]
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")


class TestCallWhisper(unittest.IsolatedAsyncioTestCase):
    async def test_success_parses_text_and_builds_multipart(self):
        session = FakeSession(FakeResponse(200, {"text": "你好"}))
        client = make_client(session, stt_provider="whisper")
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_whisper(AUDIO_B64, "audio/mpeg", "你好呀")
        self.assertEqual(text, "你好")
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertEqual(url, "https://aihubmix.com/v1/audio/transcriptions")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-key"})
        form = kwargs["data"]
        self.assertIsInstance(form, aiohttp.FormData)
        fields = {entry[0]["name"]: entry[2] for entry in form._fields}
        self.assertEqual(set(fields), {"file", "model", "prompt"})
        self.assertEqual(fields["model"], "whisper-1")
        self.assertTrue(fields["prompt"].startswith("你好呀 "))
        self.assertIn("这是一段带标点的文字", fields["prompt"])
        self.assertIn("punctuation marks", fields["prompt"])

    async def test_429_retries_then_success(self):
        logs = []
        session = FakeSession(
            FakeResponse(429, {"error": "slow down"}),
            FakeResponse(200, {"text": "你好"}),
        )
        client = STTClient(
            make_settings(stt_provider="whisper"),
            debug_log=logs.append,
            session_provider=lambda: session,
        )
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_whisper(AUDIO_B64, "audio/mpeg", "")
        self.assertEqual(text, "你好")
        self.assertEqual(len(session.posts), 2)
        self.assertTrue(any("Whisper 429，第1次重试，等待0.01s" in m for m in logs))

    async def test_tmp_file_cleaned_up_after_call(self):
        old_tempdir = tempfile.tempdir
        try:
            with tempfile.TemporaryDirectory() as td:
                tempfile.tempdir = td
                session = FakeSession(FakeResponse(200, {"text": "你好"}))
                client = make_client(
                    session, stt_provider="whisper", api_url="https://x.com/v1"
                )
                with patch("stt_client.backoff_sec", return_value=0.01):
                    text = await client.call_whisper(AUDIO_B64, "audio/mpeg", "")
                self.assertEqual(text, "你好")
                leftovers = [n for n in os.listdir(td) if n.startswith("gsv_whisper_")]
                self.assertEqual(leftovers, [])
        finally:
            tempfile.tempdir = old_tempdir

    async def test_whisper_strips_gemini_suffix_from_base(self):
        session = FakeSession(FakeResponse(200, {"text": "你好"}))
        client = make_client(
            session, stt_provider="whisper", api_url="https://aihubmix.com/gemini"
        )
        with patch("stt_client.backoff_sec", return_value=0.01):
            await client.call_whisper(AUDIO_B64, "audio/mpeg", "")
        url, _ = session.posts[0]
        self.assertEqual(url, "https://aihubmix.com/v1/audio/transcriptions")


class TestCallSttDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_whisper_provider_dispatches_to_whisper(self):
        session = FakeSession(FakeResponse(200, {"text": "你好"}))
        client = make_client(session, stt_provider="whisper")
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_stt(AUDIO_B64, "audio/mpeg", "")
        self.assertEqual(text, "你好")
        self.assertTrue(session.posts[0][0].endswith("/v1/audio/transcriptions"))

    async def test_gemini_provider_dispatches_to_gemini(self):
        session = FakeSession(FakeResponse(200, GEMINI_OK))
        client = make_client(session, stt_provider="gemini")
        with patch("stt_client.backoff_sec", return_value=0.01):
            text = await client.call_stt(AUDIO_B64, "audio/mpeg", "")
        self.assertEqual(text, "你好")
        self.assertIn("generateContent", session.posts[0][0])


if __name__ == "__main__":
    unittest.main()
