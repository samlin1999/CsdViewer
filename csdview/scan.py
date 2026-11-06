"""全專案檢查：在背景把每個 .csd 跑一次 doctor，檔案列表上直接標出哪些檔有問題。

重點是「不能拖慢電腦」：
- 只有一條執行緒、一次一個檔，檔與檔之間讓出 CPU（伺服器本身也已經是低優先權）；
- 使用者正在看的那一格在算圖時先讓它（預抓不算，否則小檔整段預抓期間永遠輪不到）；
- 網頁沒在看（分頁切走、關掉）超過 PAUSE_AFTER 秒就停，回來再接著跑；
- 結果依檔案指紋存到 ~/.csdview_scan/，下次開只重查有改過的檔。

已知的盲點：指紋只看 .csd 本身。貼圖換掉、被引用的 .csd 改過時這個檔不會自動重查，
按「重新檢查全部」即可。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

# 檢查規則改了就加一：存在磁碟上的舊結果整份作廢，避免沿用舊規則的判斷
CHECK_VERSION = 3

PAUSE_AFTER = 20.0          # 網頁多久沒來問就暫停（秒）
SAVE_EVERY = 25             # 每查幾個檔寫一次磁碟


def _cache_dir() -> str:
    return (os.environ.get("CSDVIEW_SCAN_DIR")
            or os.path.join(os.path.expanduser("~"), ".csdview_scan"))


class ProjectScan:
    def __init__(self, state):
        self.state = state              # server.ViewerState
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.results: dict[str, dict] = {}
        self.seq = 0
        self.status = "idle"            # idle / running / paused / done
        self.user_paused = False
        self.done = 0
        self.total = 0
        self.current = ""
        self.started = 0.0
        self.finished = 0.0
        self._force = False
        self._last_poll = time.monotonic()
        key = hashlib.sha1(os.path.normcase(state.root).encode("utf-8")).hexdigest()[:16]
        self._cache_path = os.path.join(_cache_dir(), key + ".json")
        self._load()

    # ------------------------------------------------------------ 磁碟快取
    def _load(self) -> None:
        try:
            with open(self._cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("version") != CHECK_VERSION or data.get("root") != self.state.root:
            return
        for rel, entry in (data.get("results") or {}).items():
            if isinstance(entry, dict) and "stamp" in entry:
                self.seq += 1
                self.results[rel] = {**entry, "seq": self.seq}

    def _save(self) -> None:
        with self._lock:
            results = {rel: {k: v for k, v in entry.items() if k != "seq"}
                       for rel, entry in self.results.items()}
        payload = {"version": CHECK_VERSION, "root": self.state.root, "results": results}
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            tmp = self._cache_path + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self._cache_path)
        except OSError:
            pass                        # 存不了就下次重查，不影響這次

    # ------------------------------------------------------------ 結果
    def record(self, full: str, summary: dict | None, stamp, error: str = "") -> None:
        """記下一個檔的檢查結果。單檔檢查（使用者打開檔案）也會走這裡，列表跟著更新。"""
        try:
            rel = os.path.relpath(full, self.state.root).replace("\\", "/")
        except ValueError:
            return
        if rel.startswith(".."):
            return
        entry = {"stamp": list(stamp)}
        if error:
            entry.update({"e": 1, "w": 0, "i": 0, "error": error})
        elif summary is not None:
            entry.update({"e": summary.get("errors", 0), "w": summary.get("warnings", 0),
                          "i": summary.get("infos", 0)})
            kinds = [g["kind"] for g in summary.get("groups", [])]
            if kinds:
                entry["kinds"] = kinds
        with self._lock:
            old = self.results.get(rel)
            if old and {k: v for k, v in old.items() if k != "seq"} == entry:
                return
            self.seq += 1
            entry["seq"] = self.seq
            self.results[rel] = entry

    # ------------------------------------------------------------ 控制
    def start(self, force: bool = False) -> None:
        with self._lock:
            if force:
                self._force = True
            self.user_paused = False
            self._last_poll = time.monotonic()
            # _thread 只在同一把鎖裡清掉（_run_once 結束時），所以不會發生「正要結束、
            # 這邊以為它還活著」而把重查的要求弄丟
            # 當下就標成執行中：不然在執行緒真的開跑前來問的人（網頁、測試）會看到上一趟的「完成」
            self.status = "running"
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="csdview-scan",
                                                daemon=True)
                self._thread.start()
        self._wake.set()

    def pause(self, paused: bool) -> None:
        with self._lock:
            self.user_paused = paused
        self._wake.set()

    def poll(self, since: int = 0) -> dict:
        """網頁每隔幾秒來問一次；只回 seq 比 since 新的結果。"""
        with self._lock:
            self._last_poll = time.monotonic()
            changed = {rel: entry for rel, entry in self.results.items() if entry["seq"] > since}
            payload = {"status": self.status, "userPaused": self.user_paused,
                       "done": self.done, "total": self.total, "current": self.current,
                       "seq": self.seq, "results": changed,
                       "seconds": round((self.finished or time.monotonic()) - self.started, 1)
                       if self.started else 0}
        self._wake.set()
        return payload

    # ------------------------------------------------------------ 背景執行緒
    def _should_wait(self) -> str:
        """回傳要等的理由；空字串＝可以繼續。"""
        if self.user_paused:
            return "paused"
        if time.monotonic() - self._last_poll > PAUSE_AFTER:
            return "paused"
        if self.state.interactive_busy():
            return "yield"
        return ""

    def _wait_turn(self) -> None:
        while True:
            reason = self._should_wait()
            if not reason:
                with self._lock:
                    self.status = "running"
                return
            if reason == "paused":
                with self._lock:
                    self.status = "paused"
                self._wake.wait(2.0)
                self._wake.clear()
            else:
                time.sleep(0.1)

    def _run(self) -> None:
        # 跑的途中又按了「重新檢查全部」就再來一趟（這條執行緒還活著，start() 不會另開）
        while self._run_once():
            pass

    def _run_once(self) -> bool:
        with self._lock:
            force, self._force = self._force, False
            self.status = "running"
            self.started = time.monotonic()
            self.finished = 0.0
            self.done = 0
        files = [f["path"] for f in self.state.list_files(fresh=True)]
        existing = set(files)
        with self._lock:
            self.total = len(files)
            for gone in [rel for rel in self.results if rel not in existing]:
                del self.results[gone]
        since_save = 0
        for rel in files:
            self._wait_turn()
            with self._lock:
                self.current = rel
            full = os.path.join(self.state.root, rel)
            try:
                st = os.stat(full)
                stamp = [st.st_mtime_ns, st.st_size]
            except OSError:
                with self._lock:
                    self.done += 1
                continue
            known = self.results.get(rel)
            if not force and known and known.get("stamp") == stamp:
                with self._lock:
                    self.done += 1
                continue
            try:
                self.state.check(full)                 # 會呼叫 record()
            except Exception as exc:                   # 壞檔不能讓整趟停下來
                self.record(full, None, stamp, f"{type(exc).__name__}: {exc}")
            with self._lock:
                self.done += 1
            since_save += 1
            if since_save >= SAVE_EVERY:
                self._save()
                since_save = 0
            time.sleep(0.03)                           # 讓出 CPU，介面與其他程式先走
        self._save()
        with self._lock:
            self.current = ""
            self.status = "done"
            self.finished = time.monotonic()
            again = self._force
            if not again:
                self._thread = None
        return again
