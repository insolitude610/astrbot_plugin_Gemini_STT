"""
纯网络模块：Gemini / Whisper STT 调用（URL 规范化、鉴权、重试）。

仅依赖标准库 + aiohttp，不依赖 astrbot，便于单元测试。
"""

import asyncio
import base64
import json
import os
import random
import re
import tempfile
from typing import Callable, Optional

import aiohttp

from .settings import PluginSettings


class RetryableStatusError(Exception):
    """可重试的 HTTP 状态（>=600 或 429），携带原始响应文本。"""

    def __init__(self, status: int, raw: str = ""):
        super().__init__(f"status {status}")
        self.status = status
        self.raw = raw


class RetryableNetworkError(Exception):
    """瞬态网络/IO 异常（可重试）。"""


def _normalize_api_base(api_url: str, *, strip_gemini_suffix: bool = False) -> str:
    base = (api_url or "").rstrip("/")
    if base.endswith("/v1/chat/completions"):
        base = base[: -len("/v1/chat/completions")]
    elif base.endswith("/v1"):
        base = base[: -len("/v1")]
    elif strip_gemini_suffix and base.endswith("/gemini"):
        base = base[: -len("/gemini")]
    return base


def _build_auth(api_key_header: str, api_key: str, *, query_key_in_url: bool) -> tuple:
    """Returns (headers_dict, url_query_param). query_key_in_url=True (Gemini): key goes in URL.
    query_key_in_url=False (Whisper ORIGINAL QUIRK, must be preserved): query mode still sends
    Authorization: Bearer header and NO URL param."""
    if api_key_header == "query":
        if query_key_in_url:
            return {}, f"key={api_key}"
        return {"Authorization": f"Bearer {api_key}"}, ""
    if api_key_header == "x-api-key":
        return {"x-api-key": api_key}, ""
    if api_key_header == "api-key":
        return {"api-key": api_key}, ""
    return {"Authorization": f"Bearer {api_key}"}, ""


def backoff_sec(i: int) -> float:
    return min(2**i, 8) + random.uniform(0, 0.3)


def normalize_model_name(model: str) -> str:
    model = (model or "").strip()
    if "/" in model:
        model = model.split("/")[-1].strip()
    model = re.sub(r"^\[[^\]]+\]\s*", "", model).strip()
    if model.startswith("models/"):
        model = model[len("models/") :]
    return model or "gemini-2.0-flash"


class STTClient:
    def __init__(
        self,
        settings: PluginSettings,
        debug_log: Optional[Callable[[str], None]] = None,
        session_provider: Optional[Callable[[], aiohttp.ClientSession]] = None,
    ):
        self.settings = settings
        self.debug_log = debug_log or (lambda msg: None)
        self.session_provider = session_provider

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session_provider is not None:
            return self.session_provider()
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        return aiohttp.ClientSession(timeout=timeout, trust_env=False)

    def build_gemini_url(self, api_url: str, model: str) -> str:
        base = _normalize_api_base(api_url)
        return f"{base}/v1beta/models/{model}:generateContent"

    def build_stt_instruction(self) -> str:
        custom = (self.settings.voice_instruction or "").strip()
        if custom:
            return custom

        if self.settings.output_mode == "rich":
            return (
                "你是一个极为敏感的语音分析器，就像把耳机放在某个场景里被动聆听。"
                "无论音频是否有人说话，都必须完整输出以下5项（不可省略任何一项）：\n"
                "1) 原话转写：若有人声则逐字转写；若无人声则写【用户未说话】\n"
                "2) 语言：识别到的语言，若无人声则写【不适用】\n"
                "3) 语气/情绪：说话时的情绪；若无人声则写【不适用】\n"
                "4) 环境音：描述音频中可感知的背景声音特征，60字以内，帮助判断录音所处场景。\n"
                "5) 大意总结：综合以上内容用一句话描述这段音频，30字以内。\n"
                "不要回答用户，不要对上述内容做任何解释，严格按格式输出。"
            )

        return (
            "你是一个极为敏感的语音转写器。"
            "若音频中有人说话，直接输出带合适标点符号的原话纯文本，无需任何格式。"
            "若音频中无人说话，输出一句简短描述，例如：用户未说话，环境为轻微键盘声、室内安静。"
            "不要加任何标题、编号或Markdown格式。"
        )

    async def call_stt(self, audio_b64: str, audio_mime: str, user_text: str) -> str:
        if self.settings.stt_provider == "whisper":
            return await self.call_whisper(audio_b64, audio_mime, user_text)
        return await self.call_gemini(audio_b64, audio_mime, user_text)

    async def call_whisper(
        self, audio_b64: str, audio_mime: str, user_text: str
    ) -> str:
        s = self.settings
        api_url = s.api_url
        api_key = s.api_key
        model = s.whisper_model or "whisper-1"

        if not api_url or not api_key:
            self.debug_log("api_url 或 api_key 未配置")
            return ""

        base = _normalize_api_base(api_url, strip_gemini_suffix=True)
        url = f"{base}/v1/audio/transcriptions"
        self.debug_log(f"Whisper URL: {url}")

        headers, _query = _build_auth(s.api_key_header, api_key, query_key_in_url=False)

        tmp_path = os.path.join(
            tempfile.gettempdir(), f"gsv_whisper_{os.urandom(4).hex()}.mp3"
        )
        try:
            audio_bytes = base64.b64decode(audio_b64)
            with open(tmp_path, "wb") as f:
                f.write(audio_bytes)

            session = self._get_session()
            try:

                async def attempt_fn() -> str:
                    try:
                        with open(tmp_path, "rb") as f:
                            form = aiohttp.FormData()
                            form.add_field(
                                "file", f, filename="audio.mp3", content_type=audio_mime
                            )
                            form.add_field("model", model)
                            prompt_parts = []
                            if user_text:
                                prompt_parts.append(user_text)
                            prompt_parts.append(
                                "这是一段带标点的文字。它能引导模型，让输出结果也带有正确的标点符号。"
                                "This is a piece of text with punctuation. It can guide the model to produce output with correct punctuation marks."
                            )
                            form.add_field("prompt", " ".join(prompt_parts))

                            async with session.post(
                                url, data=form, headers=headers
                            ) as resp:
                                raw = await resp.text()

                                if resp.status == 200:
                                    try:
                                        data = json.loads(raw)
                                    except Exception:
                                        self.debug_log(
                                            f"Whisper返回非JSON: {raw[:200]}"
                                        )
                                        return ""
                                    text = data.get("text", "")
                                    if text and text.strip():
                                        return text.strip()
                                    self.debug_log("Whisper返回空text")
                                    return ""

                                if resp.status >= 600 or resp.status == 429:
                                    raise RetryableStatusError(resp.status, raw)

                                self.debug_log(
                                    f"Whisper失败: {resp.status} - {raw[:300]}"
                                )
                                return ""
                    except RetryableStatusError:
                        raise
                    except Exception as e:
                        raise RetryableNetworkError(e) from e

                return await self._with_retry(attempt_fn, "Whisper")
            finally:
                if self.session_provider is None:
                    await session.close()
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    async def call_gemini(self, audio_b64: str, audio_mime: str, user_text: str) -> str:
        s = self.settings
        api_url = s.api_url
        api_key = s.api_key
        raw_model = s.gemini_model or getattr(s, "model", "") or "gemini-2.0-flash"
        model = (
            normalize_model_name(raw_model)
            if s.enable_model_normalize
            else raw_model.strip()
        )

        if not api_url or not api_key:
            self.debug_log("api_url 或 api_key 未配置")
            return ""

        url = self.build_gemini_url(api_url, model)
        self.debug_log(f"Gemini URL: {url}")

        headers = {"Content-Type": "application/json"}
        auth_headers, query = _build_auth(
            s.api_key_header, api_key, query_key_in_url=True
        )
        headers.update(auth_headers)
        if query:
            url += ("&" if "?" in url else "?") + query

        stt_instruction = self.build_stt_instruction()
        if user_text:
            stt_instruction += f"\n\n用户同时发送文字：{user_text}"

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": audio_mime, "data": audio_b64}},
                        {"text": stt_instruction},
                    ],
                }
            ]
        }

        session = self._get_session()
        try:

            async def attempt_fn() -> str:
                try:
                    async with session.post(url, headers=headers, json=payload) as resp:
                        raw = await resp.text()

                        if resp.status == 200:
                            try:
                                data = json.loads(raw)
                            except Exception:
                                self.debug_log(f"Gemini返回非JSON: {raw[:200]}")
                                return ""
                            cands = data.get("candidates", [])
                            if not cands:
                                self.debug_log("Gemini返回空candidates")
                                return ""
                            parts = cands[0].get("content", {}).get("parts", [])
                            for p in parts:
                                text = p.get("text")
                                if text and text.strip():
                                    return text.strip()
                            self.debug_log("Gemini返回parts中无text")
                            return ""

                        if resp.status >= 600 or resp.status == 429:
                            raise RetryableStatusError(resp.status, raw)

                        self.debug_log(f"Gemini失败: {resp.status} - {raw[:300]}")
                        return ""
                except RetryableStatusError:
                    raise
                except Exception as e:
                    raise RetryableNetworkError(e) from e

            return await self._with_retry(attempt_fn, "Gemini")
        finally:
            if self.session_provider is None:
                await session.close()

    async def _with_retry(self, attempt_fn, tag: str) -> str:
        retry_times = self.settings.retry_times
        for i in range(retry_times + 1):
            try:
                return await attempt_fn()
            except RetryableStatusError as e:
                if i < retry_times:
                    wait_sec = backoff_sec(i)
                    self.debug_log(
                        f"{tag} {e.status}，第{i + 1}次重试，等待{wait_sec:.2f}s"
                    )
                    await asyncio.sleep(wait_sec)
                    continue
                self.debug_log(f"{tag}失败: {e.status} - {e.raw[:300]}")
                return ""
            except RetryableNetworkError as e:
                if i < retry_times:
                    wait_sec = backoff_sec(i)
                    self.debug_log(f"{tag}异常重试({i + 1}): {e}，等待{wait_sec:.2f}s")
                    await asyncio.sleep(wait_sec)
                    continue
                self.debug_log(f"{tag}异常: {e}")
                return ""
            except Exception as e:
                self.debug_log(f"{tag}异常: {e}")
                return ""
        return ""
