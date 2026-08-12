import asyncio
import os
import tempfile
import time
from unittest import IsolatedAsyncioTestCase

from cleanup import TempCleaner


class FakeSettings:
    def __init__(
        self,
        enable: bool = True,
        on_start: bool = True,
        interval: int = 0,
        max_age: int = 300,
    ):
        self.enable_temp_cleanup = enable
        self.temp_cleanup_on_start = on_start
        self.temp_cleanup_interval_sec = interval
        self.temp_cleanup_max_age_sec = max_age


class TempCleanerTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(_rmtree, self.tmpdir)
        self.log = []
        self.settings = FakeSettings()

    def make_cleaner(self, settings=None, dirs=None):
        return TempCleaner(
            settings if settings is not None else self.settings,
            debug_log=self.log.append,
            dirs=dirs if dirs is not None else {self.tmpdir},
        )

    def make_file(self, name: str, age_sec: float = 0.0):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f:
            f.write(b"x")
        if age_sec > 0:
            os.utime(path, (time.time() - age_sec, time.time() - age_sec))
        return path

    def make_standard_files(self):
        self.make_file("gsv_old.mp3", age_sec=100)
        self.make_file("gsv_url_new.mp3", age_sec=0)
        self.make_file("gsv_record_x.mp3", age_sec=100)
        self.make_file("other.mp3", age_sec=100)

    def gsv_files_left(self):
        return sorted(n for n in os.listdir(self.tmpdir) if n.startswith("gsv_"))

    # ---------------- cleanup_temp_files ----------------

    def test_cleanup_zero_age_deletes_all_gsv_prefixed(self):
        self.make_standard_files()
        cleaner = self.make_cleaner()
        removed = cleaner.cleanup_temp_files(0)
        self.assertEqual(removed, 3)
        self.assertEqual(self.gsv_files_left(), [])
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "other.mp3")))

    def test_cleanup_older_than_keeps_new_file(self):
        self.make_standard_files()
        cleaner = self.make_cleaner()
        removed = cleaner.cleanup_temp_files(older_than_sec=50)
        self.assertEqual(removed, 2)
        self.assertEqual(self.gsv_files_left(), ["gsv_url_new.mp3"])
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "other.mp3")))

    def test_cleanup_disabled_returns_zero_and_deletes_nothing(self):
        self.make_standard_files()
        cleaner = self.make_cleaner(settings=FakeSettings(enable=False))
        removed = cleaner.cleanup_temp_files(0)
        self.assertEqual(removed, 0)
        self.assertEqual(len(os.listdir(self.tmpdir)), 4)

    def test_cleanup_skips_nonexistent_dir_without_error(self):
        cleaner = self.make_cleaner(dirs={os.path.join(self.tmpdir, "missing")})
        self.assertEqual(cleaner.cleanup_temp_files(0), 0)

    # ---------------- start ----------------

    async def test_start_bootstrap_cleans_once_and_no_task_when_interval_zero(self):
        self.make_file("gsv_old.mp3", age_sec=100)
        self.settings.temp_cleanup_on_start = True
        self.settings.temp_cleanup_interval_sec = 0
        cleaner = self.make_cleaner()
        cleaner.start()
        cleaner.start()
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "gsv_old.mp3")))
        self.assertEqual(self.log.count("启动清理完成，删除临时文件: 1"), 1)
        self.assertIsNone(cleaner._cleanup_task)

    async def test_start_disabled_marks_bootstrapped_without_cleanup(self):
        self.make_file("gsv_old.mp3", age_sec=100)
        cleaner = self.make_cleaner(settings=FakeSettings(enable=False, on_start=True))
        cleaner.start()
        self.assertTrue(cleaner._bootstrapped)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "gsv_old.mp3")))

    async def test_start_creates_task_when_interval_positive(self):
        self.settings.temp_cleanup_interval_sec = 60
        cleaner = self.make_cleaner()
        cleaner.start()
        self.assertIsNotNone(cleaner._cleanup_task)
        task = cleaner._cleanup_task
        cleaner.start()
        self.assertIs(cleaner._cleanup_task, task)
        cleaner._cleanup_task.cancel()

    # ---------------- stop ----------------

    async def test_stop_on_unstarted_cleaner_no_error(self):
        cleaner = self.make_cleaner()
        await cleaner.stop()
        await cleaner.stop()

    async def test_stop_cancels_running_task(self):
        self.settings.temp_cleanup_interval_sec = 1
        cleaner = self.make_cleaner()
        cleaner.start()
        task = cleaner._cleanup_task
        self.assertIsNotNone(task)
        await asyncio.sleep(0.1)
        await cleaner.stop()
        self.assertTrue(task.done())
        self.assertIsNone(task.exception())


def _rmtree(path):
    if os.path.exists(path):
        import shutil

        shutil.rmtree(path, ignore_errors=True)
