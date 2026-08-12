import os
import tempfile
import unittest

from settings import PluginSettings


def _normalize_dirs(raw):
    out = []
    for d in raw or []:
        try:
            rp = os.path.realpath(str(d))
            if os.path.isdir(rp) and rp not in out:
                out.append(rp)
        except Exception:
            continue
    return out


class TestPluginSettingsDefaults(unittest.TestCase):
    def test_from_config_empty_dict_produces_all_defaults(self):
        s = PluginSettings.from_config({})
        expected_dir_defaults = _normalize_dirs(
            [
                os.path.abspath("data"),
                os.path.abspath("data/temp"),
                tempfile.gettempdir(),
            ]
        )
        self.assertFalse(s.debug)
        self.assertTrue(s.enable_voice)
        self.assertEqual(s.ffmpeg_path, "")
        self.assertFalse(s.enable_group_voice)
        self.assertEqual(s.group_voice_whitelist, [])
        self.assertFalse(s.stop_other_handlers)
        self.assertEqual(s.stop_event_timing, "never")
        self.assertEqual(s.on_stt_fail, "notify_pass")
        self.assertEqual(s.output_mode, "simple")
        self.assertTrue(s.attach_voice_marker)
        self.assertTrue(s.attach_speaker_meta)
        self.assertFalse(s.show_transcript)
        self.assertTrue(s.enable_model_normalize)
        self.assertTrue(s.enable_transcript_clean)
        self.assertEqual(s.max_transcript_chars, 2000)
        self.assertEqual(s.max_audio_mb, 20)
        self.assertEqual(s.timeout_sec, 120)
        self.assertEqual(s.retry_times, 2)
        self.assertEqual(s.voice_file_wait_sec, 10)
        self.assertFalse(s.bypass_local_file)
        self.assertTrue(s.enable_get_record_fallback)
        self.assertTrue(s.allow_napcat_local_record_url)
        self.assertEqual(s.api_key_header, "bearer")
        self.assertEqual(s.stt_provider, "gemini")
        self.assertFalse(s.enable_punctuation)
        self.assertEqual(s.punctuation_provider_id, "")
        self.assertEqual(s.path_remap_from, "")
        self.assertEqual(s.path_remap_to, "")
        self.assertTrue(s.use_current_conversation)
        self.assertTrue(s.use_framework_tool_manager)
        self.assertFalse(s.allow_remote_audio_url)
        self.assertEqual(s.remote_audio_domain_whitelist, [])
        self.assertTrue(s.block_private_network)
        self.assertTrue(s.strict_local_path_check)
        self.assertEqual(s.local_audio_allowed_dirs, expected_dir_defaults)
        self.assertTrue(s.enable_temp_cleanup)
        self.assertTrue(s.temp_cleanup_on_start)
        self.assertEqual(s.temp_cleanup_interval_sec, 1800)
        self.assertEqual(s.temp_cleanup_max_age_sec, 300)
        self.assertTrue(s.temp_cleanup_on_terminate)
        self.assertEqual(s.api_url, "")
        self.assertEqual(s.api_key, "")
        self.assertEqual(s.gemini_model, "gemini-2.0-flash")
        self.assertEqual(s.whisper_model, "whisper-1")
        self.assertEqual(s.voice_instruction, "")


class TestPluginSettingsEnumValidation(unittest.TestCase):
    def _warn_recorder(self):
        return []

    def test_invalid_stop_event_timing_falls_back_and_warns(self):
        warnings = []
        s = PluginSettings.from_config(
            {"stop_event_timing": "weird"}, warn_log=warnings.append
        )
        self.assertEqual(s.stop_event_timing, "never")
        self.assertEqual(len(warnings), 1)
        self.assertIn("stop_event_timing", warnings[0])
        self.assertIn("weird", warnings[0])

    def test_invalid_on_stt_fail_falls_back_and_warns(self):
        warnings = []
        s = PluginSettings.from_config(
            {"on_stt_fail": "weird"}, warn_log=warnings.append
        )
        self.assertEqual(s.on_stt_fail, "notify_pass")
        self.assertEqual(len(warnings), 1)
        self.assertIn("on_stt_fail", warnings[0])
        self.assertIn("weird", warnings[0])

    def test_invalid_output_mode_falls_back_and_warns(self):
        warnings = []
        s = PluginSettings.from_config(
            {"output_mode": "weird"}, warn_log=warnings.append
        )
        self.assertEqual(s.output_mode, "simple")
        self.assertEqual(len(warnings), 1)
        self.assertIn("output_mode", warnings[0])
        self.assertIn("weird", warnings[0])

    def test_invalid_stt_provider_falls_back_and_warns(self):
        warnings = []
        s = PluginSettings.from_config(
            {"stt_provider": "weird"}, warn_log=warnings.append
        )
        self.assertEqual(s.stt_provider, "gemini")
        self.assertEqual(len(warnings), 1)
        self.assertIn("stt_provider", warnings[0])
        self.assertIn("weird", warnings[0])

    def test_invalid_api_key_header_falls_back_and_warns(self):
        warnings = []
        s = PluginSettings.from_config(
            {"api_key_header": "weird"}, warn_log=warnings.append
        )
        self.assertEqual(s.api_key_header, "bearer")
        self.assertEqual(len(warnings), 1)
        self.assertIn("api_key_header", warnings[0])
        self.assertIn("weird", warnings[0])

    def test_valid_enum_values_pass_without_warning(self):
        warnings = []
        s = PluginSettings.from_config(
            {
                "stop_event_timing": "after_stt",
                "on_stt_fail": "block",
                "output_mode": "rich",
                "stt_provider": "gemini",
                "api_key_header": "x-api-key",
            },
            warn_log=warnings.append,
        )
        self.assertEqual(s.stop_event_timing, "after_stt")
        self.assertEqual(s.on_stt_fail, "block")
        self.assertEqual(s.output_mode, "rich")
        self.assertEqual(s.stt_provider, "gemini")
        self.assertEqual(s.api_key_header, "x-api-key")
        self.assertEqual(warnings, [])

    def test_warn_log_defaults_to_noop(self):
        s = PluginSettings.from_config({"stop_event_timing": "weird"})
        self.assertEqual(s.stop_event_timing, "never")


class TestPluginSettingsBehavior(unittest.TestCase):
    def test_whisper_plus_rich_coerces_to_simple(self):
        s = PluginSettings.from_config(
            {"stt_provider": "whisper", "output_mode": "rich"}
        )
        self.assertEqual(s.stt_provider, "whisper")
        self.assertEqual(s.output_mode, "simple")

    def test_empty_string_gemini_model_falls_back(self):
        s = PluginSettings.from_config({"gemini_model": ""})
        self.assertEqual(s.gemini_model, "gemini-2.0-flash")

    def test_empty_string_api_url_and_api_key_preserved(self):
        s = PluginSettings.from_config({"api_url": "", "api_key": ""})
        self.assertEqual(s.api_url, "")
        self.assertEqual(s.api_key, "")

    def test_gemini_model_legacy_model_key_fallback(self):
        s = PluginSettings.from_config({"model": "legacy-model"})
        self.assertEqual(s.gemini_model, "legacy-model")

    def test_debug_mode_key_maps_to_debug_field(self):
        self.assertTrue(PluginSettings.from_config({"debug_mode": True}).debug)
        self.assertTrue(PluginSettings.from_config({"debug_mode": "x"}).debug)

    def test_group_voice_whitelist_coerced_to_strings(self):
        s = PluginSettings.from_config({"group_voice_whitelist": [123, "456"]})
        self.assertEqual(s.group_voice_whitelist, ["123", "456"])

    def test_bool_and_int_coercion(self):
        s = PluginSettings.from_config(
            {"enable_voice": 0, "timeout_sec": "60", "retry_times": 5}
        )
        self.assertFalse(s.enable_voice)
        self.assertEqual(s.timeout_sec, 60)
        self.assertEqual(s.retry_times, 5)

    def test_local_audio_allowed_dirs_filtered_and_realpathed(self):
        s = PluginSettings.from_config(
            {
                "local_audio_allowed_dirs": [
                    "does_not_exist_xyz",
                    tempfile.gettempdir(),
                ]
            }
        )
        self.assertEqual(
            s.local_audio_allowed_dirs, [os.path.realpath(tempfile.gettempdir())]
        )


if __name__ == "__main__":
    unittest.main()
