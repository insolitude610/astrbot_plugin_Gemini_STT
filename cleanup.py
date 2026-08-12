"""临时文件清理模块：清理插件产生的 gsv_* 临时文件（仅标准库）。"""

import asyncio
import os
import tempfile
import time


class TempCleaner:
    """清理插件临时文件（仅 gsv_* 前缀）。

    settings 需提供 enable_temp_cleanup / temp_cleanup_on_start /
    temp_cleanup_interval_sec / temp_cleanup_max_age_sec 四个属性。
    debug_log 为可选的可调用对象（接收一条消息），默认不输出。
    """

    def __init__(self, settings, debug_log=None, dirs=None):
        self._settings = settings
        self._log = debug_log if debug_log is not None else self._noop_log
        self._cleanup_task: asyncio.Task | None = None
        self._bootstrapped = False
        self._prefixes = ("gsv_", "gsv_url_", "gsv_record_")
        if dirs is not None:
            self._dirs = {os.path.realpath(d) for d in dirs}
        else:
            self._dirs = {
                os.path.realpath(tempfile.gettempdir()),
                os.path.realpath(os.path.abspath("data/temp")),
            }

    @staticmethod
    def _noop_log(msg):
        pass

    def start(self) -> None:
        """首次启动时做一次清理并启动定时任务（懒启动，幂等）"""
        if self._bootstrapped:
            return
        self._bootstrapped = True

        if not self._settings.enable_temp_cleanup:
            return

        if self._settings.temp_cleanup_on_start:
            removed = self.cleanup_temp_files(older_than_sec=0)
            self._log(f"启动清理完成，删除临时文件: {removed}")

        if self._settings.temp_cleanup_interval_sec > 0:
            try:
                self._cleanup_task = asyncio.create_task(self._periodic_loop())
                self._log(
                    f"已启动定时清理任务，间隔={self._settings.temp_cleanup_interval_sec}s"
                )
            except Exception as e:
                self._log(f"启动定时清理任务失败: {e}")

    async def stop(self) -> None:
        try:
            if self._cleanup_task and not self._cleanup_task.done():
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except Exception:
                    pass
        except Exception as e:
            self._log(f"stop cancel cleanup task error: {e}")

    async def _periodic_loop(self):
        try:
            while True:
                await asyncio.sleep(max(1, self._settings.temp_cleanup_interval_sec))
                removed = self.cleanup_temp_files(
                    older_than_sec=max(0, self._settings.temp_cleanup_max_age_sec)
                )
                if removed > 0:
                    self._log(f"定时清理完成，删除临时文件: {removed}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._log(f"定时清理异常: {e}")

    def cleanup_temp_files(self, older_than_sec: int = 0) -> int:
        """清理插件临时文件（仅 gsv_* 前缀）"""
        if not self._settings.enable_temp_cleanup:
            return 0

        now = time.time()
        removed = 0

        for d in self._dirs:
            if not os.path.isdir(d):
                continue
            try:
                for name in os.listdir(d):
                    if not name.startswith(self._prefixes):
                        continue
                    path = os.path.join(d, name)
                    if not os.path.isfile(path):
                        continue

                    try:
                        if older_than_sec > 0:
                            age = now - os.path.getmtime(path)
                            if age < older_than_sec:
                                continue
                        os.remove(path)
                        removed += 1
                    except Exception as e:
                        self._log(f"删除临时文件失败: {path}, err={e}")
            except Exception as e:
                self._log(f"扫描清理目录失败: {d}, err={e}")

        return removed
