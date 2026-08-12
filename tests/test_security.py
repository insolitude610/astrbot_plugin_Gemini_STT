"""单元测试：security 模块（SSRF + 本地路径安全检查的纯逻辑）"""

import asyncio
import os
import tempfile
import unittest

import security


class TestIsPrivateIp(unittest.TestCase):
    def test_private_and_special_ips_are_private(self):
        for ip in (
            "127.0.0.1",  # loopback
            "10.0.0.1",  # RFC1918
            "192.168.1.1",  # RFC1918
            "169.254.1.1",  # link-local
            "224.0.0.1",  # multicast
            "::1",  # IPv6 loopback
            "fd00::1",  # IPv6 ULA
            "not-an-ip",  # 解析失败 -> 保守返回 True
        ):
            with self.subTest(ip=ip):
                self.assertTrue(security.is_private_ip(ip))

    def test_public_ip_is_not_private(self):
        self.assertFalse(security.is_private_ip("8.8.8.8"))


class TestHostAllowedByWhitelist(unittest.TestCase):
    def test_subdomain_matches_base_domain(self):
        self.assertTrue(
            security.host_allowed_by_whitelist("a.example.com", ["example.com"])
        )

    def test_dot_prefixed_whitelist(self):
        self.assertTrue(
            security.host_allowed_by_whitelist("a.example.com", [".example.com"])
        )

    def test_unrelated_domain_rejected(self):
        self.assertFalse(
            security.host_allowed_by_whitelist("a.example.com", ["sub.example.com"])
        )

    def test_empty_whitelist_allows_all(self):
        self.assertTrue(security.host_allowed_by_whitelist("anything.example.com", []))

    def test_case_insensitive(self):
        self.assertTrue(
            security.host_allowed_by_whitelist("A.EXAMPLE.COM", ["Example.com"])
        )


class TestIsLoopbackHost(unittest.TestCase):
    def test_loopback_hosts(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                self.assertTrue(security.is_loopback_host(host))

    def test_public_host_is_not_loopback(self):
        self.assertFalse(security.is_loopback_host("8.8.8.8"))


class TestRemapLocalPath(unittest.TestCase):
    def test_manual_remap(self):
        result = security.remap_local_path(
            "/app/.config/QQ/cache/1.silk",
            manual_from="/app/.config/QQ",
            manual_to="/root/ntqq",
            auto_pairs=[],
        )
        self.assertEqual(result, "/root/ntqq/cache/1.silk")

    def test_manual_wins_over_auto_pairs(self):
        result = security.remap_local_path(
            "/app/.config/QQ/cache/1.silk",
            manual_from="/app/.config/QQ",
            manual_to="/manual/dst",
            auto_pairs=[("/app/.config/QQ", "/auto/dst")],
        )
        self.assertEqual(result, "/manual/dst/cache/1.silk")

    def test_unmatched_path_returned_unchanged(self):
        path = "/no/match/file.silk"
        self.assertEqual(
            security.remap_local_path(
                path,
                manual_from="/app/.config/QQ",
                manual_to="/root/ntqq",
                auto_pairs=[("/other", "/dst")],
            ),
            path,
        )

    def test_backslash_normalization(self):
        result = security.remap_local_path(
            r"\app\.config\QQ\cache\1.silk",
            manual_from=r"\app\.config\QQ",
            manual_to="/root/ntqq",
            auto_pairs=[],
        )
        self.assertEqual(result, "/root/ntqq/cache/1.silk")


class TestIsSafeLocalAudioPath(unittest.TestCase):
    def setUp(self):
        self._paths_to_remove = []

    def addCleanupPath(self, path):
        self._paths_to_remove.append(path)

    def tearDown(self):
        for p in self._paths_to_remove:
            try:
                os.remove(p)
            except OSError:
                pass

    def _make_file(self, dirpath, name):
        path = os.path.join(dirpath, name)
        with open(path, "wb") as f:
            f.write(b"x")
        return path

    def test_gsv_temp_file_allowed_regardless_of_whitelist(self):
        tmp_dir = os.path.realpath(tempfile.gettempdir())
        fd, name = tempfile.mkstemp(prefix="gsv_", suffix=".wav", dir=tmp_dir)
        os.close(fd)
        self.addCleanupPath(name)
        self.assertTrue(
            security.is_safe_local_audio_path(
                name, strict=True, allowed_dirs=[], warn_log=None
            )
        )

    def test_txt_extension_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._make_file(td, "voice.txt")
            self.assertFalse(
                security.is_safe_local_audio_path(
                    path, strict=True, allowed_dirs=[], warn_log=None
                )
            )

    def test_file_inside_allowed_dir(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._make_file(td, "voice.wav")
            self.assertTrue(
                security.is_safe_local_audio_path(
                    path, strict=True, allowed_dirs=[td], warn_log=None
                )
            )

    def test_file_outside_whitelist_false_and_warn_logged(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as other,
        ):
            path = self._make_file(td, "voice.wav")
            warns = []
            result = security.is_safe_local_audio_path(
                path, strict=True, allowed_dirs=[other], warn_log=warns.append
            )
            self.assertFalse(result)
            self.assertEqual(len(warns), 1)
            self.assertIn("voice.wav", warns[0])

    def test_strict_false_allows_existing_audio_file(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as other,
        ):
            path = self._make_file(td, "voice.wav")
            self.assertTrue(
                security.is_safe_local_audio_path(
                    path, strict=False, allowed_dirs=[other], warn_log=None
                )
            )


class TestBuildAutoRemapPairs(unittest.TestCase):
    def test_manual_config_disables_auto_detection(self):
        self.assertEqual(
            security.build_auto_remap_pairs(
                ["/app/.config/QQ"], ["/root/ntqq"], manual_from="/a", manual_to="/b"
            ),
            [],
        )

    def test_src_eq_dst_pair_skipped(self):
        self.assertEqual(
            security.build_auto_remap_pairs(["/same"], ["/same"], "", ""), []
        )

    def test_only_existing_dst_dirs_used(self):
        with tempfile.TemporaryDirectory() as td:
            pairs = security.build_auto_remap_pairs(
                ["/app/src", "/other/src"],
                [td, "/definitely/not/exist"],
                "",
                "",
            )
            expected = ("/app/src", td.rstrip("/\\"))
            self.assertIn(expected, pairs)
            for _src, dst in pairs:
                self.assertNotIn("/definitely/not/exist", dst)


class TestAutoDiscoverAllowedDirs(unittest.TestCase):
    def _with_roots(self, roots):
        orig = security.NAPCAT_COMMON_ROOTS
        security.NAPCAT_COMMON_ROOTS = roots
        self.addCleanup(setattr, security, "NAPCAT_COMMON_ROOTS", orig)

    def test_existing_root_added_and_returned(self):
        with tempfile.TemporaryDirectory() as td:
            self._with_roots([td, "/definitely/not/exist"])
            allowed = []
            added = security.auto_discover_allowed_dirs(allowed)
            self.assertEqual(added, [os.path.realpath(td)])
            self.assertEqual(allowed, [os.path.realpath(td)])

    def test_non_existent_root_skipped(self):
        self._with_roots(["/definitely/not/exist"])
        allowed = []
        added = security.auto_discover_allowed_dirs(allowed)
        self.assertEqual(added, [])
        self.assertEqual(allowed, [])

    def test_existing_root_already_allowed_not_readded(self):
        with tempfile.TemporaryDirectory() as td:
            self._with_roots([td])
            allowed = [os.path.realpath(td)]
            added = security.auto_discover_allowed_dirs(allowed)
            self.assertEqual(added, [])
            self.assertEqual(allowed, [os.path.realpath(td)])


class TestPrepareRemoteTarget(unittest.TestCase):
    def run_coro(self, coro):
        return asyncio.run(coro)

    def test_disabled_returns_false_none(self):
        ok, info = self.run_coro(
            security.prepare_remote_target(
                "http://a.example.com/x",
                allow_remote_audio_url=False,
                domain_whitelist=[],
                block_private_network=True,
            )
        )
        self.assertFalse(ok)
        self.assertIsNone(info)

    def test_non_http_scheme_rejected(self):
        ok, _ = self.run_coro(
            security.prepare_remote_target(
                "ftp://example.com/x",
                allow_remote_audio_url=True,
                domain_whitelist=[],
                block_private_network=True,
            )
        )
        self.assertFalse(ok)

    def test_hostname_localhost_rejected(self):
        ok, _ = self.run_coro(
            security.prepare_remote_target(
                "http://localhost/x",
                allow_remote_audio_url=True,
                domain_whitelist=[],
                block_private_network=True,
            )
        )
        self.assertFalse(ok)

    def test_domain_not_in_whitelist_rejected(self):
        ok, _ = self.run_coro(
            security.prepare_remote_target(
                "http://evil-site.com/x",
                allow_remote_audio_url=True,
                domain_whitelist=["example.com"],
                block_private_network=False,
            )
        )
        self.assertFalse(ok)

    def test_private_ip_literal_blocked(self):
        ok, _ = self.run_coro(
            security.prepare_remote_target(
                "http://127.0.0.1/x",
                allow_remote_audio_url=True,
                domain_whitelist=[],
                block_private_network=True,
            )
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
