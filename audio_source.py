"""audio_source 模块：音频来源解析（纯逻辑 + aiohttp，无 astrbot 依赖）

从 main.py 中抽取的音频来源相关逻辑：
- 固定 DNS 解析器（缓解 DNS rebinding / TOCTOU）
- 远程音频下载（SSRF 安全校验 + 静态解析器 + 拒绝重定向 + 大小限制）
- get_record 兜底路径解析（Windows/Linux 结构差异、base64 解码、路径映射）
- 原始语音路径解析（等待落盘、安全检查、大小限制、兜底）
- 语音文件 → base64 编码（格式探测 + 转 MP3 + 临时文件清理）

依赖兄弟模块 security（URL/路径安全）与 audio_convert（格式探测/转换）。
日志通过注入的 debug_log / warn_log / info_log 回调输出（默认 None 即忽略）。
"""

import asyncio
import base64
import os
import socket
import tempfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from . import security
from .audio_convert import (
    PILK_AVAILABLE,
    convert_silk_to_pcm,
    convert_to_mp3,
    detect_audio_format,
    encode_mp3_b64_with_limit,
)
from .settings import PluginSettings


class StaticResolver(aiohttp.abc.AbstractResolver):
    """
    将 host 固定解析到预先校验过的 IP 列表，缓解 DNS rebinding / TOCTOU。
    """

    def __init__(self, host_ip_map: Dict[str, List[str]]):
        self._host_ip_map = {k.lower(): v[:] for k, v in host_ip_map.items()}

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        ips = self._host_ip_map.get((host or "").lower(), [])
        if not ips:
            raise OSError(f"resolver: no ip for host {host}")

        results = []
        for ip in ips:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": fam,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return results

    async def close(self):
        return


class AudioSourceResolver:
    """音频来源解析器：远程下载 / get_record 兜底 / 本地路径解析 / base64 编码"""

    def __init__(
        self,
        settings: PluginSettings,
        debug_log=None,
        warn_log=None,
        info_log=None,
    ):
        self.settings = settings
        self._debug_log = debug_log
        self._warn_log = warn_log
        self._info_log = info_log
        self._auto_remap_pairs = self.build_auto_remap_pairs()

    # ---------------- 内部小工具（日志 / 大小 / 安全 / 转换） ----------------

    def _d(self, msg: str) -> None:
        if self._debug_log:
            self._debug_log(msg)

    def _i(self, msg: str) -> None:
        if self._info_log:
            self._info_log(msg)

    def _file_size_ok(self, size_bytes: int) -> bool:
        return size_bytes <= self.settings.max_audio_mb * 1024 * 1024

    def _is_safe_local_audio_path(self, path: str) -> bool:
        return security.is_safe_local_audio_path(
            path,
            strict=self.settings.strict_local_path_check,
            allowed_dirs=self.settings.local_audio_allowed_dirs,
            warn_log=self._warn_log,
            debug_log=self._debug_log,
        )

    def _convert_to_mp3(
        self, input_path: str, input_format: Optional[str] = None
    ) -> str:
        return convert_to_mp3(
            self.settings.ffmpeg_path,
            input_path,
            input_format=input_format,
            timeout=30,
            debug_log=self._debug_log,
        )

    def _convert_silk_to_pcm(self, silk_path: str, pcm_path: str) -> bool:
        return convert_silk_to_pcm(silk_path, pcm_path, debug_log=self._debug_log)

    # ---------------- 远程下载 ----------------

    async def download_remote_audio(self, url: str) -> str:
        safe, target = await security.prepare_remote_target(
            url,
            allow_remote_audio_url=self.settings.allow_remote_audio_url,
            domain_whitelist=self.settings.remote_audio_domain_whitelist,
            block_private_network=self.settings.block_private_network,
            debug_log=self._debug_log,
        )
        if not safe or not target:
            self._d("远程语音URL被安全策略拦截")
            return ""

        host = target["host"]
        ips = target["ips"]

        suffix = ".bin"
        for ext in [".mp3", ".wav", ".amr", ".silk"]:
            if ext in url.lower():
                suffix = ext
                break

        tmp_path = os.path.join(
            tempfile.gettempdir(), f"gsv_url_{os.urandom(4).hex()}{suffix}"
        )

        resolver = StaticResolver({host: ips})
        connector = aiohttp.TCPConnector(resolver=resolver, ssl=True, limit=4)

        try:
            timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
            async with aiohttp.ClientSession(
                timeout=timeout, connector=connector, trust_env=False
            ) as session:
                async with session.get(
                    url, allow_redirects=False, headers={"Host": host}
                ) as resp:
                    if 300 <= resp.status < 400:
                        self._d(f"远程语音下载拒绝重定向: status={resp.status}")
                        return ""
                    if resp.status != 200:
                        self._d(f"远程语音下载失败: {resp.status}")
                        return ""

                    data = await resp.read()

            if not self._file_size_ok(len(data)):
                self._d(f"远程语音超大小限制: {len(data)} bytes")
                return ""

            with open(tmp_path, "wb") as f:
                f.write(data)

            return tmp_path
        except Exception as e:
            self._d(f"远程语音下载异常: {e}")
            return ""
        finally:
            try:
                await resolver.close()
            except Exception as e:
                self._d(f"resolver close error: {e}")

    async def download_trusted_record_url(self, url: str) -> str:
        """
        专用于 get_record 返回的 URL：
        block_private_network=True 时，允许按配置放行回环地址。
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return ""

            host = (parsed.hostname or "").strip().lower()
            if not host:
                return ""

            if self.settings.block_private_network:
                if not security.is_loopback_host(host):
                    self._d(f"trusted_record_url 非回环地址，拦截: {host}")
                    return ""
                if not self.settings.allow_napcat_local_record_url:
                    self._d(f"trusted_record_url 回环地址被策略禁用: {host}")
                    return ""

            timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
            tmp_path = os.path.join(
                tempfile.gettempdir(), f"gsv_record_{os.urandom(4).hex()}.mp3"
            )

            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=False
            ) as session:
                async with session.get(url, allow_redirects=False) as resp:
                    if 300 <= resp.status < 400:
                        self._d(f"trusted_record_url 拒绝重定向: {resp.status}")
                        return ""
                    if resp.status != 200:
                        self._d(f"trusted_record_url 下载失败: {resp.status}")
                        return ""
                    data = await resp.read()

            if not self._file_size_ok(len(data)):
                self._d(f"trusted_record_url 音频超限: {len(data)} bytes")
                return ""

            with open(tmp_path, "wb") as f:
                f.write(data)

            return tmp_path
        except Exception as e:
            self._d(f"trusted_record_url 下载异常: {e}")
            return ""

    # ---------------- get_record 兜底 ----------------

    def extract_record_file_token(self, record_comp) -> str:
        for key in ("file", "id", "path", "url"):
            v = getattr(record_comp, key, None)
            if v:
                return str(v).strip()

        data = getattr(record_comp, "data", None)
        if isinstance(data, dict):
            for key in ("file", "id", "path", "url"):
                v = data.get(key)
                if v:
                    return str(v).strip()
        return ""

    async def get_record_fallback_path(self, event, record_comp) -> str:
        """本地路径不可读时，通过 NapCat get_record 兜底"""
        if not self.settings.enable_get_record_fallback:
            return ""

        try:
            if event.get_platform_name() != "aiocqhttp":
                return ""
            if not hasattr(event, "bot") or not hasattr(event.bot, "api"):
                return ""

            token = self.extract_record_file_token(record_comp)
            if not token:
                self._d("get_record兜底：无法提取 token")
                return ""

            result = await event.bot.api.call_action(
                "get_record", file=token, out_format="mp3"
            )

            # 完整打印原始返回，便于诊断 Linux/NapCat 差异
            self._d(f"get_record原始返回: {str(result)[:600]}")

            if not isinstance(result, dict):
                self._d("get_record兜底：返回非dict")
                return ""

            # NapCat 有两种结构：
            #   Windows: {"data": {"file": "...", ...}}
            #   Linux:   {"file": "...", "base64": "...", ...}（扁平，无data包装）
            data_inner = result.get("data", None)
            if isinstance(data_inner, dict) and data_inner:
                lookup = data_inner  # Windows 结构
            elif isinstance(data_inner, str) and data_inner.strip():
                # data 本身就是路径字符串
                target = self.remap_local_path(data_inner.strip())
                p = os.path.realpath(os.path.abspath(target))
                if os.path.exists(p):
                    return p
                self._d(f"get_record data字符串路径不存在: {p}")
                return ""
            else:
                lookup = result  # Linux 扁平结构，直接用顶层

            # ① 优先：用 base64 字段直接解码（Linux NapCat 最可靠的方式）
            b64 = str(lookup.get("base64", "") or "").strip()
            if b64:
                try:
                    audio_data = base64.b64decode(b64)
                    if audio_data and self._file_size_ok(len(audio_data)):
                        tmp_path = os.path.join(
                            tempfile.gettempdir(),
                            f"gsv_record_{os.urandom(4).hex()}.mp3",
                        )
                        with open(tmp_path, "wb") as f:
                            f.write(audio_data)
                        self._d(
                            f"get_record兜底：base64解码成功 -> {tmp_path} ({len(audio_data)} bytes)"
                        )
                        return tmp_path
                    else:
                        self._d(
                            f"get_record兜底：base64解码后为空或超限 ({len(audio_data)} bytes)"
                        )
                except Exception as e:
                    self._d(f"get_record兜底：base64解码失败: {e}")

            # ② 备选：用路径字段（需做路径前缀替换）
            target = ""
            PATH_KEYS = (
                "file",
                "path",
                "url",
                "file_path",
                "localPath",
                "local_path",
                "filename",
            )
            for key in PATH_KEYS:
                v = lookup.get(key)
                if v and isinstance(v, str) and v.strip():
                    target = v.strip()
                    self._d(f"get_record兜底：命中字段 [{key}] = {target[:200]}")
                    break

            target = str(target or "").strip()
            if not target:
                self._i("[GeminiSTTBridge] get_record兜底：返回中无 base64/file/path/url")
                self._d(f"get_record完整返回结构: {result}")
                return ""

            if target.startswith("http://") or target.startswith("https://"):
                host = urlparse(target).hostname or ""
                if security.is_loopback_host(host):
                    return await self.download_trusted_record_url(target)
                return await self.download_remote_audio(target)

            # 本地路径：先做前缀替换再检查存在性
            target = self.remap_local_path(target)
            p = os.path.realpath(os.path.abspath(target))
            if os.path.exists(p):
                return p

            self._d(f"get_record返回本地路径不存在: {p}")
            return ""
        except Exception as e:
            self._d(f"get_record兜底异常: {e}")
            return ""

    # ---------------- 原始语音路径解析 ----------------

    async def resolve_original_audio_path(self, event, record_comp) -> str:
        path_attr = getattr(record_comp, "path", None) or getattr(
            record_comp, "url", None
        )
        raw = str(path_attr).strip().strip('"').strip("'") if path_attr else ""

        # 1) 组件直接给URL
        if raw.startswith("http://") or raw.startswith("https://"):
            p = await self.download_remote_audio(raw)
            if p:
                return p

        # 2) 本地路径等待落盘（先做路径前缀替换，解决多容器挂载路径不一致问题）
        if raw and not self.settings.bypass_local_file:
            raw = self.remap_local_path(raw)
            original_path = os.path.realpath(os.path.abspath(raw))
            wait_sec = max(0, self.settings.voice_file_wait_sec)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_sec

            while True:
                if os.path.exists(original_path):
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(0.26)

            if os.path.exists(original_path):
                if not self._is_safe_local_audio_path(original_path):
                    return ""
                size = os.path.getsize(original_path)
                if not self._file_size_ok(size):
                    self._d(f"本地语音超大小限制: {size} bytes")
                    return ""
                return original_path

            self._d(f"语音文件不存在(等待{wait_sec}s后): {original_path}")

        # 3) get_record兜底
        fallback = await self.get_record_fallback_path(event, record_comp)
        if not fallback:
            return ""

        if not self._is_safe_local_audio_path(fallback):
            return ""

        size = os.path.getsize(fallback)
        if not self._file_size_ok(size):
            self._d(f"兜底语音超大小限制: {size} bytes")
            return ""

        self._d(f"get_record兜底成功: {fallback}")
        return fallback

    # ---------------- 语音数据获取 ----------------

    async def get_voice_data(self, event, record_comp) -> Tuple[Optional[str], Optional[str]]:
        temp_files_to_clean: List[str] = []
        try:
            original_path = await self.resolve_original_audio_path(event, record_comp)
            if not original_path:
                return None, None

            if os.path.realpath(original_path).startswith(
                os.path.realpath(tempfile.gettempdir())
            ):
                temp_files_to_clean.append(original_path)

            fmt = detect_audio_format(original_path)
            self._d(f"音频格式: {fmt}")

            if fmt == "mp3":
                return encode_mp3_b64_with_limit(
                    original_path,
                    self.settings.max_audio_mb,
                    debug_log=self._debug_log,
                )

            if fmt in ("wav", "amr"):
                if not self.settings.ffmpeg_path:
                    self._d("未找到FFmpeg，无法转换 wav/amr")
                    return None, None

                mp3_path = self._convert_to_mp3(original_path)
                if not mp3_path:
                    return None, None
                temp_files_to_clean.append(mp3_path)
                return encode_mp3_b64_with_limit(
                    mp3_path,
                    self.settings.max_audio_mb,
                    debug_log=self._debug_log,
                )

            if fmt == "silk":
                if not PILK_AVAILABLE:
                    self._d("未安装pilk，无法解码silk")
                    return None, None
                if not self.settings.ffmpeg_path:
                    self._d("未找到FFmpeg，无法转换silk")
                    return None, None

                pcm_path = os.path.join(
                    tempfile.gettempdir(), f"gsv_{os.urandom(4).hex()}.pcm"
                )
                temp_files_to_clean.append(pcm_path)

                if not self._convert_silk_to_pcm(original_path, pcm_path):
                    return None, None

                mp3_path = self._convert_to_mp3(pcm_path, input_format="pcm")
                if not mp3_path:
                    return None, None
                temp_files_to_clean.append(mp3_path)
                return encode_mp3_b64_with_limit(
                    mp3_path,
                    self.settings.max_audio_mb,
                    debug_log=self._debug_log,
                )

            return None, None
        except Exception as e:
            self._d(f"获取语音失败: {e}")
            return None, None
        finally:
            for fp in temp_files_to_clean:
                try:
                    if fp and os.path.exists(fp):
                        os.remove(fp)
                except Exception as e:
                    self._d(f"清理临时文件失败: {fp}, err={e}")

    # ---------------- 路径映射与白名单 ----------------

    def build_auto_remap_pairs(self) -> list:
        return security.build_auto_remap_pairs(
            security.NAPCAT_KNOWN_SOURCES,
            security.NAPCAT_KNOWN_DESTS,
            self.settings.path_remap_from,
            self.settings.path_remap_to,
        )

    def auto_discover_allowed_dirs(self) -> list:
        """自动将磁盘上已存在的 NapCat/QQ 目录加入路径白名单（自适应），
        就地更新 settings.local_audio_allowed_dirs，并返回新增列表。"""
        return security.auto_discover_allowed_dirs(
            self.settings.local_audio_allowed_dirs, info_log=self._info_log
        )

    def remap_local_path(self, path: str) -> str:
        """路径前缀替换：先用手动配置，再尝试自动映射对。
        解决 NapCat 上报容器内路径与 AstrBot 实际挂载路径不一致的问题（自适应）。
        """
        return security.remap_local_path(
            path,
            self.settings.path_remap_from,
            self.settings.path_remap_to,
            self._auto_remap_pairs,
            debug_log=self._debug_log,
        )
