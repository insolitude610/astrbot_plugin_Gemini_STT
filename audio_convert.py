"""
音频转换与编码工具模块 (stdlib only, 无 astrbot 依赖)
- 音频格式探测 / FFmpeg 查找 / SILK->PCM / 转 MP3 / MP3->base64(带大小限制)
"""

import os
import base64
import shutil
import tempfile
import subprocess
from typing import Optional, Tuple

try:
    import pilk

    PILK_AVAILABLE = True
except ImportError:
    PILK_AVAILABLE = False


def detect_audio_format(file_path: str) -> str:
    try:
        with open(file_path, "rb") as f:
            header = f.read(32)

        if b"SILK" in header:
            return "silk"
        if header.startswith(b"#!AMR"):
            return "amr"
        if header.startswith(b"ID3") or (
            len(header) > 1 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
        ):
            return "mp3"
        if header.startswith(b"RIFF") and b"WAVE" in header[:12]:
            return "wav"
        return "unknown"
    except Exception:
        return "unknown"


def find_ffmpeg(custom_path: str = "", debug_log=None) -> str:
    custom = str(custom_path or "").strip()
    if custom:
        if os.path.isfile(custom) and os.access(custom, os.X_OK):
            bn = os.path.basename(custom).lower()
            if bn in ("ffmpeg", "ffmpeg.exe"):
                return custom
        if debug_log:
            debug_log(f"自定义ffmpeg_path不可执行或不存在: {custom}")

    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = shutil.which(name)
    if found:
        bn = os.path.basename(found).lower()
        if bn in ("ffmpeg", "ffmpeg.exe"):
            return found

    try:
        r = subprocess.run([name, "-version"], capture_output=True, timeout=6)
        if r.returncode == 0:
            return name
    except Exception:
        pass
    return ""


def convert_silk_to_pcm(silk_path: str, pcm_path: str, debug_log=None) -> bool:
    if not PILK_AVAILABLE:
        return False
    try:
        pilk.decode(silk_path, pcm_path)
        return os.path.exists(pcm_path) and os.path.getsize(pcm_path) > 0
    except Exception as e:
        if debug_log:
            debug_log(f"SILK解码失败: {e}")
        return False


def convert_to_mp3(
    ffmpeg_path: str,
    input_path: str,
    input_format: Optional[str] = None,
    timeout: int = 30,
    debug_log=None,
) -> str:
    if not ffmpeg_path:
        return ""

    mp3_path = os.path.join(tempfile.gettempdir(), f"gsv_{os.urandom(4).hex()}.mp3")
    try:
        if input_format == "pcm":
            cmd = [
                ffmpeg_path,
                "-y",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-i",
                input_path,
                "-c:a",
                "libmp3lame",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                mp3_path,
            ]
        else:
            cmd = [
                ffmpeg_path,
                "-y",
                "-i",
                input_path,
                "-c:a",
                "libmp3lame",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-b:a",
                "64k",
                mp3_path,
            ]

        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if (
            r.returncode == 0
            and os.path.isfile(mp3_path)
            and os.path.getsize(mp3_path) > 0
        ):
            return mp3_path

        err = r.stderr.decode(errors="ignore")[:300] if r.stderr else "unknown"
        if debug_log:
            debug_log(f"转MP3失败: {err}")
        return ""
    except Exception as e:
        if debug_log:
            debug_log(f"转MP3异常: {e}")
        return ""


def encode_mp3_b64_with_limit(
    mp3_path: str, max_audio_mb: int, debug_log=None
) -> Tuple[Optional[str], Optional[str]]:
    if not os.path.isfile(mp3_path):
        return None, None
    size = os.path.getsize(mp3_path)
    if size > max_audio_mb * 1024 * 1024:
        if debug_log:
            debug_log(f"MP3超大小限制: {size} bytes")
        return None, None
    with open(mp3_path, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode(), "audio/mpeg"
