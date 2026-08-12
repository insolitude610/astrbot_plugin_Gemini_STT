"""security 模块：SSRF 防护与本地路径安全检查（纯逻辑，无框架依赖）

从 main.py 中抽取的纯函数：
- 远程 URL 安全（白名单、私网拦截、DNS 解析）
- 本地语音路径安全（扩展名白名单、目录白名单、建议目录）
- NapCat 容器路径映射与白名单自适应

所有日志输出通过注入的 debug_log / info_log / warn_log 回调（默认 None 即忽略），
模块自身不依赖 astrbot / aiohttp。
"""

import asyncio
import ipaddress
import os
import socket
import tempfile
from urllib.parse import urlparse

# NapCat 容器内常见路径（上报路径）
NAPCAT_KNOWN_SOURCES: list[str] = [
    "/app/.config/QQ",
    "/app/QQ",
    "/opt/QQ",
]
# AstrBot 侧常见挂载路径（实际可访问路径）
NAPCAT_KNOWN_DESTS: list[str] = [
    "/root/astrbot/ntqq",
    "/root/.config/QQ",
    "/home/user/.config/QQ",
    "/var/lib/QQ",
    "/data/QQ",
    "/app/.config/QQ",  # 同容器部署时 src==dst，无需映射但要放白名单
]
# 所有可能独立存在的 NapCat/QQ 数据根目录（用于白名单自动发现）
NAPCAT_COMMON_ROOTS: list[str] = [
    "/app/.config/QQ",
    "/root/astrbot/ntqq",
    "/root/.config/QQ",
    "/home/user/.config/QQ",
    "/var/lib/QQ",
    "/data/QQ",
    "/opt/QQ",
]


def normalize_allowed_dirs(raw_dirs, debug_log=None) -> list[str]:
    out = []
    for d in raw_dirs or []:
        try:
            rp = os.path.realpath(str(d))
            if os.path.isdir(rp) and rp not in out:
                out.append(rp)
        except Exception:
            continue
    return out


def auto_discover_allowed_dirs(
    allowed_dirs, info_log=None, debug_log=None
) -> list[str]:
    """自动将磁盘上已存在的 NapCat/QQ 目录加入路径白名单（自适应）"""
    added = []
    for p in NAPCAT_COMMON_ROOTS:
        try:
            rp = os.path.realpath(p)
            if os.path.isdir(rp) and rp not in allowed_dirs:
                allowed_dirs.append(rp)
                added.append(rp)
        except Exception:
            continue
    if added and info_log:
        info_log(f"[GeminiSTTBridge] 自动放行目录（自适应）: {added}")
    return added


def build_auto_remap_pairs(
    sources: list[str], dests: list[str], manual_from: str, manual_to: str
) -> list[tuple[str, str]]:
    """自动构建路径映射对：NapCat上报路径 → AstrBot实际路径（自适应）
    仅在用户未手动配置 path_remap_from/to 时生效。
    """
    if manual_from and manual_to:
        return []  # 用户已手动配置，跳过自动检测

    pairs = []
    for src in sources:
        for dst in dests:
            if src == dst:
                continue  # 同路径无需映射
            if os.path.isdir(os.path.realpath(dst)):
                pair = (src.rstrip("/\\"), dst.rstrip("/\\"))
                if pair not in pairs:
                    pairs.append(pair)
    return pairs


def remap_local_path(
    path: str,
    manual_from: str,
    manual_to: str,
    auto_pairs: list[tuple[str, str]],
    debug_log=None,
) -> str:
    """路径前缀替换：先用手动配置，再尝试自动映射对。
    解决 NapCat 上报容器内路径与 AstrBot 实际挂载路径不一致的问题（自适应）。
    """
    if not path:
        return path

    norm = path.replace("\\", "/")

    # 1. 用户显式配置优先
    if manual_from and manual_to:
        src = manual_from.rstrip("/\\")
        dst = manual_to.rstrip("/\\")
        norm_src = src.replace("\\", "/")
        if norm.startswith(norm_src + "/") or norm == norm_src:
            remapped = dst + norm[len(norm_src) :]
            if debug_log:
                debug_log(f"路径前缀替换(手动配置): {path} → {remapped}")
            return remapped

    # 2. 自动映射
    for src, dst in auto_pairs:
        norm_src = src.replace("\\", "/")
        if norm.startswith(norm_src + "/") or norm == norm_src:
            remapped = dst + norm[len(norm_src) :]
            if debug_log:
                debug_log(f"路径前缀替换(自动): {path} → {remapped}")
            return remapped

    return path


# ---------------- URL安全（SSRF） ----------------


def is_private_ip(ip: str) -> bool:
    try:
        obj = ipaddress.ip_address(ip)
        return (
            obj.is_private
            or obj.is_loopback
            or obj.is_link_local
            or obj.is_reserved
            or obj.is_multicast
        )
    except Exception:
        return True


def host_allowed_by_whitelist(host: str, whitelist: list[str]) -> bool:
    if not whitelist:
        return True
    host = (host or "").lower().strip()
    for item in whitelist:
        w = item.lower().strip()
        if not w:
            continue
        if w.startswith("."):
            if host.endswith(w):
                return True
        else:
            if host == w or host.endswith("." + w):
                return True
    return False


async def resolve_host_ips(host: str, port: int) -> set[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return {x[4][0] for x in infos if x and x[4]}


async def prepare_remote_target(
    url: str,
    *,
    allow_remote_audio_url: bool,
    domain_whitelist: list[str],
    block_private_network: bool,
    debug_log=None,
) -> tuple[bool, dict | None]:
    if not allow_remote_audio_url:
        return False, None

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, None

    host = (parsed.hostname or "").lower().strip()
    if not host or host == "localhost":
        return False, None

    if not host_allowed_by_whitelist(host, domain_whitelist):
        if debug_log:
            debug_log(f"远程域名不在白名单: {host}")
        return False, None

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        ipaddress.ip_address(host)
        if block_private_network and is_private_ip(host):
            if debug_log:
                debug_log(f"远程IP被私网策略拦截: {host}")
            return False, None
        return True, {"parsed": parsed, "host": host, "port": port, "ips": [host]}
    except Exception:
        pass

    try:
        ips = list(await resolve_host_ips(host, port))
        if not ips:
            return False, None

        if block_private_network:
            for ip in ips:
                if is_private_ip(ip):
                    if debug_log:
                        debug_log(f"远程域名解析到私网IP，拦截: {host} -> {ip}")
                    return False, None

        return True, {"parsed": parsed, "host": host, "port": port, "ips": ips}
    except Exception as e:
        if debug_log:
            debug_log(f"远程域名解析失败: {host}, err={e}")
        return False, None


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except Exception:
        return False


# ---------------- 本地路径安全 ----------------


def suggest_allowed_dirs(blocked_path: str, allowed_dirs: list[str]) -> list[str]:
    p = os.path.realpath(blocked_path)
    suggestions: list[str] = []

    d = os.path.dirname(p)
    if d:
        suggestions.append(d)

    norm = p.replace("\\", "/")
    idx = norm.lower().find("/ptt/")
    if idx != -1:
        ptt_root = norm[: idx + len("/ptt")].replace("/", os.sep)
        if ptt_root:
            suggestions.append(os.path.realpath(ptt_root))

    out = []
    for s in suggestions:
        s = os.path.realpath(s)
        if s not in out and s not in allowed_dirs:
            out.append(s)
    return out


def is_safe_local_audio_path(
    path: str,
    *,
    strict: bool,
    allowed_dirs: list[str],
    warn_log=None,
    debug_log=None,
) -> bool:
    rp = os.path.realpath(path)
    if not os.path.isfile(rp):
        return False

    # 放行插件自身临时文件
    tmp_dir = os.path.realpath(tempfile.gettempdir())
    bn = os.path.basename(rp)
    if rp.startswith(tmp_dir + os.sep) and (
        bn.startswith("gsv_")
        or bn.startswith("gsv_url_")
        or bn.startswith("gsv_record_")
    ):
        return True

    ext = os.path.splitext(rp)[1].lower()
    if ext not in (".mp3", ".wav", ".amr", ".silk", ".pcm", ".bin"):
        if debug_log:
            debug_log(f"本地语音后缀不在允许列表: {rp}")
        return False

    if not strict:
        return True

    for base in allowed_dirs:
        try:
            if os.path.commonpath([rp, base]) == base:
                return True
        except Exception:
            continue

    suggestions = suggest_allowed_dirs(rp, allowed_dirs)
    best = suggestions[-1] if suggestions else os.path.dirname(rp)
    if warn_log:
        warn_log(
            f"[GeminiSTTBridge] 语音路径未放行: {rp}\n"
            f"  → 请将此目录加入配置项 local_audio_allowed_dirs: {best}"
        )
    return False
