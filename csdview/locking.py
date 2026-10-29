"""跨行程的檔案鎖與原子換檔。

跟 mcpServer 的 `csdmcp.locking` 同一套規則，兩邊各自一份、互不 import：
鎖的是 `<path>.lock` 而不是目標本身（目標會被 os.replace 換掉）。
登錄檔 `~/.csdview_projects` 兩邊都會寫，所以鎖檔路徑必須一致。
"""
from __future__ import annotations

import contextlib
import os
import time

if os.name == "nt":
    import msvcrt

    def _acquire(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


REPLACE_ATTEMPTS = 6


class ReplaceBusy(PermissionError):
    pass


def replace_with_retry(temp: str, target: str, attempts: int = REPLACE_ATTEMPTS) -> None:
    for attempt in range(attempts):
        try:
            os.replace(temp, target)
            return
        except PermissionError as exc:
            if attempt == attempts - 1:
                raise ReplaceBusy(
                    f"{os.path.basename(target)} 一直被別的程式開著，"
                    f"重試 {attempts} 次都換不進去。"
                ) from exc
            time.sleep(0.02 * (attempt + 1))


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 10.0):
    """鎖住 `<path>.lock`。"""
    lock_path = path + ".lock"
    folder = os.path.dirname(lock_path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    deadline = time.monotonic() + timeout
    handle = open(lock_path, "a+b")
    try:
        while not _acquire(handle):
            if time.monotonic() > deadline:
                raise LockTimeout(
                    f"等 {timeout:g} 秒還拿不到 {os.path.basename(path)} 的鎖。")
            time.sleep(0.05)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()
