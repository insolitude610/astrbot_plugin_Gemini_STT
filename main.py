"""
Gemini STT Bridge Plugin (Hardened + Refactored)
- 仅负责语音 -> 文本（simple/rich）并转发给框架
- 非语音不干预
- 支持失败策略、事件拦截时机、模型名清洗、说话人信息注入
"""

from typing import List, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger

from .settings import PluginSettings
from .transcript import (
    clean_transcript,
    extract_plain_transcript,
    is_instruction_hallucination,
)
from .audio_convert import PILK_AVAILABLE, find_ffmpeg
from .cleanup import TempCleaner
from .stt_client import STTClient
from .audio_source import AudioSourceResolver


@register("Gemini_STT", "政ひかりはる", "Gemini语音转写桥接到框架LLM", "2.3.7")
class GeminiSTTBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings.from_config(
            self.config, warn_log=lambda m: logger.warning(f"[GeminiSTTBridge] {m}")
        )
        self.debug = self.settings.debug
        self.ffmpeg_path = find_ffmpeg(self.settings.ffmpeg_path, debug_log=self._d)
        self._session: Optional[aiohttp.ClientSession] = None
        self.audio_source = AudioSourceResolver(
            self.settings,
            debug_log=self._d,
            warn_log=lambda m: logger.warning(m),
            info_log=lambda m: logger.info(m),
        )
        self.stt_client = STTClient(
            self.settings, debug_log=self._d, session_provider=self._get_session
        )
        self.cleaner = TempCleaner(self.settings, debug_log=self._d)
        self._auto_discover_allowed_dirs()
        self._auto_remap_pairs: List[tuple] = self.audio_source.build_auto_remap_pairs()

        logger.info("[GeminiSTTBridge] 插件已加载 v2.3.7")
        logger.info(
            f"[GeminiSTTBridge] enable_voice={self.settings.enable_voice}, "
            f"output_mode={self.settings.output_mode}, fail={self.settings.on_stt_fail}, "
            f"stop={self.settings.stop_event_timing}/{self.settings.stop_other_handlers}"
        )
        logger.info(
            f"[GeminiSTTBridge] ffmpeg={'✓' if self.ffmpeg_path else '✗'}, "
            f"pilk={'✓' if PILK_AVAILABLE else '✗'}"
        )
        if str(self.config.get("stt_provider")) == "whisper" and str(
            self.config.get("output_mode")
        ) == "rich":
            logger.info("[GeminiSTTBridge] Whisper引擎不支持rich模式，已自动切换为simple")
        if self._auto_remap_pairs:
            logger.info(
                f"[GeminiSTTBridge] 自动路径映射: "
                + ", ".join(f"{s} → {d}" for s, d in self._auto_remap_pairs)
            )

    def _d(self, msg: str):
        if self.debug:
            logger.info(f"[GeminiSTTBridge] {msg}")

    def _auto_discover_allowed_dirs(self) -> None:
        """自动将磁盘上已存在的 NapCat/QQ 目录加入路径白名单（自适应）"""
        self.audio_source.auto_discover_allowed_dirs()

    # ---------------- 生命周期 ----------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout, trust_env=False)
        return self._session

    async def terminate(self):
        try:
            await self.cleaner.stop()
        except Exception as e:
            self._d(f"terminate cancel cleanup task error: {e}")

        try:
            if self.settings.temp_cleanup_on_terminate:
                removed = self.cleaner.cleanup_temp_files(older_than_sec=0)
                self._d(f"terminate 清理完成，删除临时文件: {removed}")
        except Exception as e:
            self._d(f"terminate cleanup temp error: {e}")

        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception as e:
            self._d(f"terminate close session error: {e}")

    # ---------------- 基础工具 ----------------

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        if hasattr(event, "get_group_id"):
            gid = event.get_group_id()
            if gid:
                return True

        origin = getattr(event, "unified_msg_origin", "") or ""
        if "GroupMessage" in origin or "Group" in origin:
            return True

        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message_type"):
            mt = str(event.message_obj.message_type).lower()
            if "group" in mt:
                return True
        return False

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_group_id"):
            gid = event.get_group_id()
            if gid:
                return str(gid)

        origin = getattr(event, "unified_msg_origin", "") or ""
        if "GroupMessage" in origin:
            parts = origin.split(":")
            if len(parts) >= 3:
                return parts[-1].strip()
        return ""

    def _should_process_voice(self, event: AstrMessageEvent) -> bool:
        if not self._is_group_message(event):
            return True

        if not self.settings.enable_group_voice:
            self._d("群聊语音关闭，跳过")
            return False

        if self.settings.group_voice_whitelist:
            gid = self._get_group_id(event)
            if gid not in self.settings.group_voice_whitelist:
                self._d(f"群 {gid} 不在白名单，跳过")
                return False
        return True

    def _should_stop_before_stt(self) -> bool:
        return (
            self.settings.stop_other_handlers
            and self.settings.stop_event_timing == "before_stt"
            and self.settings.on_stt_fail not in ("pass", "notify_pass")
        )

    def _should_stop_after_stt_success(self) -> bool:
        return self.settings.stop_other_handlers and self.settings.stop_event_timing in (
            "before_stt",
            "after_stt",
        )

    # ---------------- 失败策略 ----------------

    async def _handle_stt_fail(self, event: AstrMessageEvent):
        """
        on_stt_fail:
        - pass: 放行后续插件
        - block: 拦截并静默
        - notify: 拦截并提示
        - notify_pass: 提示后放行
        """
        action = self.settings.on_stt_fail

        if action == "notify":
            if self.settings.stop_other_handlers:
                event.stop_event()
            yield event.plain_result("⚠️ 语音识别失败")
            return

        if action == "block":
            if self.settings.stop_other_handlers:
                event.stop_event()
            return

        if action == "notify_pass":
            yield event.plain_result("⚠️ 语音识别失败，已放行后续插件处理。")
            return

        return

    # ---------------- 转发文本构造 ----------------

    def _build_forward_text(self, event: AstrMessageEvent, final_text: str) -> str:
        lines: List[str] = []

        if self.settings.attach_voice_marker:
            lines.append(
                "[系统自动注入] 以下是语音转写插件对用户发送的语音消息的分析结果，"
                "这不是用户输入的文字，请勿将其视为用户在和你说话，"
                "而是作为了解用户语音内容和当前环境的背景信息："
            )

        if self.settings.attach_speaker_meta:
            sender_name = (
                event.get_sender_name()
                if hasattr(event, "get_sender_name")
                else "unknown"
            )
            sender_id = (
                event.get_sender_id() if hasattr(event, "get_sender_id") else "unknown"
            )
            group_id = event.get_group_id() if hasattr(event, "get_group_id") else ""
            platform = (
                event.get_platform_name()
                if hasattr(event, "get_platform_name")
                else "unknown"
            )

            lines.append(f"说话人: {sender_name} (ID: {sender_id})")
            lines.append(
                f"场景: {'群聊 ' + str(group_id) if group_id else '私聊'} / 平台: {platform}"
            )

        lines.append(final_text.strip())
        return "\n".join(lines).strip()

    async def _get_session_context(self, event: AstrMessageEvent):
        session_id = None
        conversation = None

        if not self.settings.use_current_conversation:
            return session_id, conversation

        try:
            session_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
            )
            if session_id:
                conversation = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin, session_id
                )
        except Exception as e:
            self._d(f"获取当前会话失败: {e}")

        return session_id, conversation

    def _get_messages(self, event: AstrMessageEvent):
        """
        优先使用框架公开API，避免直接依赖 event.message_obj.message 内部结构。
        """
        if hasattr(event, "get_messages"):
            try:
                msgs = event.get_messages()
                if msgs is not None:
                    return msgs
            except Exception as e:
                self._d(f"event.get_messages() 失败，回退 message_obj.message: {e}")

        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            return event.message_obj.message or []

        return []

    def _extract_components(self, event: AstrMessageEvent):
        voice_comp = None
        text_parts = []

        for comp in self._get_messages(event):
            cname = type(comp).__name__
            if cname == "Record":
                voice_comp = comp
            elif cname == "Plain":
                txt = getattr(comp, "text", "")
                if txt and txt.strip():
                    text_parts.append(txt.strip())

        return voice_comp, " ".join(text_parts)

    def _build_final_text_by_mode(self, stt_text: str) -> str:
        if self.settings.output_mode == "simple":
            plain = extract_plain_transcript(stt_text)
            return clean_transcript(
                plain,
                enabled=self.settings.enable_transcript_clean,
                max_chars=self.settings.max_transcript_chars,
            )
        return clean_transcript(
            stt_text,
            enabled=self.settings.enable_transcript_clean,
            max_chars=self.settings.max_transcript_chars,
        )

    # ---------------- 标点修复 ----------------

    async def _restore_punctuation(self, text: str) -> str:
        if not self.settings.enable_punctuation or not text:
            return text

        try:
            from astrbot.core.provider.entities import ProviderType

            if self.settings.punctuation_provider_id:
                provider = await self.context.provider_manager.get_provider_by_id(
                    self.settings.punctuation_provider_id
                )
                if not provider:
                    self._d(
                        f"标点修复: 指定提供商 {self.settings.punctuation_provider_id} 未找到，跳过"
                    )
                    return text
                provider_id = self.settings.punctuation_provider_id
            else:
                provider = self.context.provider_manager.get_using_provider(
                    ProviderType.CHAT_COMPLETION
                )
                if not provider:
                    self._d("标点修复: 未找到AstrBot聊天提供商，跳过")
                    return text
                provider_id = provider.meta().id

            instruction = (
                "请为以下文本添加合适的标点符号（逗号、句号、问号、感叹号等），"
                "不要修改任何文字内容，不要添加额外解释，直接输出添加标点后的文本："
            )

            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"{instruction}\n\n{text}",
                system_prompt="你是一个专业的文本标点修复助手。唯一任务是给输入文本添加合适的标点符号，直接输出结果。",
            )

            result = response.completion_text
            if result and result.strip():
                self._d(f"标点修复完成: {result[:120]}")
                return result.strip()
            self._d("标点修复返回空内容")
        except Exception as e:
            self._d(f"标点修复异常: {e}")

        return text

    # ---------------- 事件入口 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_voice(self, event: AstrMessageEvent):
        try:
            self.cleaner.start()

            if not self.settings.enable_voice:
                return

            voice_comp, user_text = self._extract_components(event)
            if not voice_comp:
                return

            if not self._should_process_voice(event):
                return

            if self._should_stop_before_stt():
                event.stop_event()

            audio_b64, audio_mime = await self.audio_source.get_voice_data(
                event, voice_comp
            )
            if not audio_b64:
                async for r in self._handle_stt_fail(event):
                    yield r
                return

            stt_text = await self.stt_client.call_stt(audio_b64, audio_mime, user_text)
            stt_text = clean_transcript(
                stt_text,
                enabled=self.settings.enable_transcript_clean,
                max_chars=self.settings.max_transcript_chars,
            )

            if not stt_text:
                async for r in self._handle_stt_fail(event):
                    yield r
                return

            # 空白语音幻觉检测：若模型把指令本身当转写内容返回，
            # 不做失败处理，改为用兜底文本替换后继续发给 LLM
            # （任何语音都要处理，哪怕是空白的，也有环境音信息）
            if is_instruction_hallucination(
                stt_text,
                self.stt_client.build_stt_instruction(),
                debug_log=self._d,
            ):
                logger.warning("[GeminiSTTBridge] 检测到空白语音幻觉，使用兜底转写替代")
                stt_text = "（未检测到有效语音内容，可能为空白或静音语音）"

            if self.settings.enable_punctuation:
                stt_text = await self._restore_punctuation(stt_text)

            final_text = self._build_final_text_by_mode(stt_text)
            if not final_text:
                async for r in self._handle_stt_fail(event):
                    yield r
                return

            if self._should_stop_after_stt_success():
                event.stop_event()

            if self.settings.show_transcript:
                yield event.plain_result(f"📝 识别结果：{final_text}")

            forward_text = self._build_forward_text(event, final_text)
            self._d(f"output_mode={self.settings.output_mode}, final_len={len(final_text)}")
            self._d(f"forward_preview={forward_text[:220]}")

            session_id, conversation = await self._get_session_context(event)
            func_tool_manager = (
                self.context.get_llm_tool_manager()
                if self.settings.use_framework_tool_manager
                else None
            )

            yield event.request_llm(
                prompt=forward_text,
                func_tool_manager=func_tool_manager,
                session_id=session_id,
                contexts=[],
                conversation=conversation,
            )
        except Exception as e:
            logger.error(f"[GeminiSTTBridge] 处理失败: {e}")
