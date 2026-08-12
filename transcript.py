"""
纯逻辑模块：转写文本清理 / rich 内容提取 / 指令幻觉检测。

仅依赖标准库（re），不引入任何 astrbot 依赖，便于单元测试。
"""

import re
from typing import List, Optional, Callable


def clean_transcript(text: str, *, enabled: bool, max_chars: int) -> str:
    t = (text or "").strip()
    if not enabled:
        return t
    t = re.sub(r"\n{3,}", "\n\n", t)
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + "..."
    return t


def extract_plain_transcript(stt_text: str) -> str:
    """
    从 rich 输出中尽量提取“原话转写”
    """
    t = (stt_text or "").strip()
    if not t:
        return ""

    patterns = [
        r"(?:^|\n)\s*(?:1[.)、]\s*)?(?:\*\*)?\s*原话转写\s*(?:\*\*)?\s*[：:]\s*(.+?)"
        r"(?=\n\s*(?:\d+[.)、]\s*|(?:\*\*)?\s*(?:语言|语气|情绪|环境音|大意总结)\b)|\Z)",
        r"(?:^|\n)\s*(?:\*\*)?\s*转写\s*(?:\*\*)?\s*[：:]\s*(.+?)"
        r"(?=\n\s*(?:\d+[.)、]\s*|(?:\*\*)?\s*(?:语言|语气|情绪|环境音|大意总结)\b)|\Z)",
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE | re.DOTALL)
        if m:
            out = m.group(1).strip()
            out = re.sub(r"^\s*[-*]+\s*", "", out, flags=re.MULTILINE)
            return out

    return t


def build_hallucination_markers(instruction: str) -> List[str]:
    candidates = []
    if instruction:
        candidates.append(instruction[:20])
    candidates += ["原话转写", "语气/情绪", "环境音", "大意总结"]
    candidates += ["语音转写器", "纯文本", "不要加任何标题"]
    return [m for m in candidates if len(m) >= 3 and m in instruction]


def is_instruction_hallucination(
    stt_text: str,
    instruction: str,
    debug_log: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    检测 Gemini 是否将 STT 指令本身作为转写内容返回（空白语音幻觉）。
    当模型收到无声/空白音频时，有时会把提示词原样输出。
    """
    if not stt_text:
        return False

    markers = build_hallucination_markers(instruction)
    matched = sum(1 for m in markers if m in stt_text)
    if matched >= 2:
        if debug_log:
            debug_log(f"STT幻觉检测：转写内容疑似重复指令（命中{matched}个标记），判定为无效")
        return True
    return False
