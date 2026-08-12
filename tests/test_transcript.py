import unittest

from transcript import (
    build_hallucination_markers,
    clean_transcript,
    extract_plain_transcript,
    is_instruction_hallucination,
)

RICH_INSTRUCTION = (
    "你是一个极为敏感的语音分析器，就像把耳机放在某个场景里被动聆听。"
    "无论音频是否有人说话，都必须完整输出以下5项（不可省略任何一项）：\n"
    "1) 原话转写：若有人声则逐字转写；若无人声则写【用户未说话】\n"
    "2) 语言：识别到的语言，若无人声则写【不适用】\n"
    "3) 语气/情绪：说话时的情绪；若无人声则写【不适用】\n"
    "4) 环境音：描述音频中可感知的背景声音特征，60字以内，帮助判断录音所处场景。\n"
    "6) 大意总结：综合以上内容用一句话描述这段音频，30字以内。\n"
    "不要回答用户，不要对上述内容做任何解释，严格按格式输出。"
)

SIMPLE_INSTRUCTION = (
    "你是一个极为敏感的语音转写器。"
    "若音频中有人说话，直接输出带合适标点符号的原话纯文本，无需任何格式。"
    "若音频中无人说话，输出一句简短描述，例如：用户未说话，环境为轻微键盘声、室内安静。"
    "不要加任何标题、编号或Markdown格式。"
)


class TestCleanTranscript(unittest.TestCase):
    def test_collapses_three_or_more_newlines(self):
        self.assertEqual(clean_transcript("a\n\n\n\nb", enabled=True, max_chars=1000), "a\n\nb")

    def test_truncates_long_text_with_ellipsis_suffix(self):
        out = clean_transcript("a" * 20, enabled=True, max_chars=10)
        self.assertEqual(out, "a" * 10 + "...")

    def test_disabled_returns_unchanged(self):
        self.assertEqual(
            clean_transcript("a\n\n\n\nb", enabled=False, max_chars=10),
            "a\n\n\n\nb",
        )


class TestExtractPlainTranscript(unittest.TestCase):
    def test_extracts_verbatim_from_numbered_rich_output(self):
        self.assertEqual(
            extract_plain_transcript("1) 原话转写：你好\n2) 语言：中文"),
            "你好",
        )

    def test_extracts_verbatim_from_bold_rich_output(self):
        self.assertEqual(extract_plain_transcript("**原话转写：** 你好"), "你好")

    def test_plain_text_without_section_returned_in_full(self):
        self.assertEqual(
            extract_plain_transcript("你好，今天天气不错"),
            "你好，今天天气不错",
        )

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(extract_plain_transcript(""), "")


class TestBuildHallucinationMarkers(unittest.TestCase):
    def test_rich_instruction_yields_rich_markers(self):
        markers = build_hallucination_markers(RICH_INSTRUCTION)
        self.assertIn("原话转写", markers)
        self.assertIn("语气/情绪", markers)
        self.assertIn("环境音", markers)
        self.assertIn("大意总结", markers)
        self.assertNotIn("语音转写器", markers)

    def test_simple_instruction_yields_plain_markers(self):
        markers = build_hallucination_markers(SIMPLE_INSTRUCTION)
        self.assertIn("语音转写器", markers)
        self.assertIn("纯文本", markers)
        self.assertIn("不要加任何标题", markers)
        self.assertNotIn("原话转写", markers)

    def test_custom_instruction_yields_only_prefix(self):
        self.assertEqual(
            build_hallucination_markers("请把这段语音转写出来"),
            ["请把这段语音转写出来"],
        )

    def test_empty_instruction_yields_no_markers(self):
        self.assertEqual(build_hallucination_markers(""), [])


class TestIsInstructionHallucination(unittest.TestCase):
    def test_transcript_repeating_instruction_flagged(self):
        self.assertTrue(
            is_instruction_hallucination(SIMPLE_INSTRUCTION, SIMPLE_INSTRUCTION)
        )

    def test_normal_transcript_not_flagged(self):
        self.assertFalse(
            is_instruction_hallucination("你好，今天天气不错", SIMPLE_INSTRUCTION)
        )

    def test_single_marker_not_enough_to_flag(self):
        self.assertFalse(
            is_instruction_hallucination("环境音", RICH_INSTRUCTION)
        )

    def test_debug_log_called_when_flagged(self):
        calls = []

        def debug_log(msg):
            calls.append(msg)

        is_instruction_hallucination(SIMPLE_INSTRUCTION, SIMPLE_INSTRUCTION, debug_log)
        self.assertEqual(len(calls), 1)
        self.assertIn("STT幻觉检测", calls[0])


if __name__ == "__main__":
    unittest.main()
