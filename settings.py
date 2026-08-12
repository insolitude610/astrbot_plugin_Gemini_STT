import os
import tempfile
from dataclasses import dataclass, field


@dataclass
class PluginSettings:
    debug: bool = False
    enable_voice: bool = True
    ffmpeg_path: str = ""
    enable_group_voice: bool = False
    group_voice_whitelist: list = field(default_factory=list)
    stop_other_handlers: bool = False
    stop_event_timing: str = "never"
    on_stt_fail: str = "notify_pass"
    output_mode: str = "simple"
    attach_voice_marker: bool = True
    attach_speaker_meta: bool = True
    show_transcript: bool = False
    enable_model_normalize: bool = True
    enable_transcript_clean: bool = True
    max_transcript_chars: int = 2000
    max_audio_mb: int = 20
    timeout_sec: int = 120
    retry_times: int = 2
    voice_file_wait_sec: int = 10
    bypass_local_file: bool = False
    enable_get_record_fallback: bool = True
    allow_napcat_local_record_url: bool = True
    api_key_header: str = "bearer"
    stt_provider: str = "gemini"
    enable_punctuation: bool = False
    punctuation_provider_id: str = ""
    path_remap_from: str = ""
    path_remap_to: str = ""
    use_current_conversation: bool = True
    use_framework_tool_manager: bool = True
    allow_remote_audio_url: bool = False
    remote_audio_domain_whitelist: list = field(default_factory=list)
    block_private_network: bool = True
    strict_local_path_check: bool = True
    local_audio_allowed_dirs: list = field(default_factory=list)
    enable_temp_cleanup: bool = True
    temp_cleanup_on_start: bool = True
    temp_cleanup_interval_sec: int = 1800
    temp_cleanup_max_age_sec: int = 300
    temp_cleanup_on_terminate: bool = True
    api_url: str = ""
    api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    whisper_model: str = "whisper-1"
    voice_instruction: str = ""

    @staticmethod
    def _enum_value(cfg, key, default, allowed, warn_log):
        v = cfg.get(key) or default
        if v not in allowed:
            if warn_log:
                warn_log(f"配置项 {key} 的值 {v} 非法，已回退为 {default}")
            v = default
        return v

    @classmethod
    def from_config(cls, cfg, warn_log=None) -> "PluginSettings":
        stop_event_timing = cls._enum_value(
            cfg,
            "stop_event_timing",
            "never",
            ("before_stt", "after_stt", "never"),
            warn_log,
        )
        on_stt_fail = cls._enum_value(
            cfg,
            "on_stt_fail",
            "notify_pass",
            ("pass", "block", "notify", "notify_pass"),
            warn_log,
        )
        output_mode = cls._enum_value(
            cfg, "output_mode", "simple", ("simple", "rich"), warn_log
        )
        stt_provider = cls._enum_value(
            cfg, "stt_provider", "gemini", ("gemini", "whisper"), warn_log
        )
        api_key_header = cls._enum_value(
            cfg,
            "api_key_header",
            "bearer",
            ("bearer", "x-api-key", "api-key", "query"),
            warn_log,
        )
        if stt_provider == "whisper" and output_mode == "rich":
            output_mode = "simple"

        allowed_dirs = []
        raw_dirs = cfg.get(
            "local_audio_allowed_dirs",
            [
                os.path.abspath("data"),
                os.path.abspath("data/temp"),
                tempfile.gettempdir(),
            ],
        )
        for d in raw_dirs or []:
            try:
                rp = os.path.realpath(str(d))
                if os.path.isdir(rp) and rp not in allowed_dirs:
                    allowed_dirs.append(rp)
            except Exception:
                continue

        return cls(
            debug=bool(cfg.get("debug_mode", False)),
            enable_voice=bool(cfg.get("enable_voice", True)),
            ffmpeg_path=cfg.get("ffmpeg_path") or "",
            enable_group_voice=bool(cfg.get("enable_group_voice", False)),
            group_voice_whitelist=[
                str(x) for x in cfg.get("group_voice_whitelist", [])
            ],
            stop_other_handlers=bool(cfg.get("stop_other_handlers", False)),
            stop_event_timing=stop_event_timing,
            on_stt_fail=on_stt_fail,
            output_mode=output_mode,
            attach_voice_marker=bool(cfg.get("attach_voice_marker", True)),
            attach_speaker_meta=bool(cfg.get("attach_speaker_meta", True)),
            show_transcript=bool(cfg.get("show_transcript", False)),
            enable_model_normalize=bool(cfg.get("enable_model_normalize", True)),
            enable_transcript_clean=bool(cfg.get("enable_transcript_clean", True)),
            max_transcript_chars=int(cfg.get("max_transcript_chars", 2000)),
            max_audio_mb=int(cfg.get("max_audio_mb", 20)),
            timeout_sec=int(cfg.get("timeout_sec", 120)),
            retry_times=int(cfg.get("retry_times", 2)),
            voice_file_wait_sec=int(cfg.get("voice_file_wait_sec", 10)),
            bypass_local_file=bool(cfg.get("bypass_local_file", False)),
            enable_get_record_fallback=bool(
                cfg.get("enable_get_record_fallback", True)
            ),
            allow_napcat_local_record_url=bool(
                cfg.get("allow_napcat_local_record_url", True)
            ),
            api_key_header=api_key_header,
            stt_provider=stt_provider,
            enable_punctuation=bool(cfg.get("enable_punctuation", False)),
            punctuation_provider_id=cfg.get("punctuation_provider_id") or "",
            path_remap_from=cfg.get("path_remap_from") or "",
            path_remap_to=cfg.get("path_remap_to") or "",
            use_current_conversation=bool(cfg.get("use_current_conversation", True)),
            use_framework_tool_manager=bool(
                cfg.get("use_framework_tool_manager", True)
            ),
            allow_remote_audio_url=bool(cfg.get("allow_remote_audio_url", False)),
            remote_audio_domain_whitelist=[
                str(x) for x in cfg.get("remote_audio_domain_whitelist", [])
            ],
            block_private_network=bool(cfg.get("block_private_network", True)),
            strict_local_path_check=bool(cfg.get("strict_local_path_check", True)),
            local_audio_allowed_dirs=allowed_dirs,
            enable_temp_cleanup=bool(cfg.get("enable_temp_cleanup", True)),
            temp_cleanup_on_start=bool(cfg.get("temp_cleanup_on_start", True)),
            temp_cleanup_interval_sec=int(cfg.get("temp_cleanup_interval_sec", 1800)),
            temp_cleanup_max_age_sec=int(cfg.get("temp_cleanup_max_age_sec", 300)),
            temp_cleanup_on_terminate=bool(cfg.get("temp_cleanup_on_terminate", True)),
            api_url=cfg.get("api_url") or "",
            api_key=cfg.get("api_key") or "",
            gemini_model=cfg.get("gemini_model")
            or cfg.get("model")
            or "gemini-2.0-flash",
            whisper_model=cfg.get("whisper_model") or "whisper-1",
            voice_instruction=cfg.get("voice_instruction") or "",
        )
