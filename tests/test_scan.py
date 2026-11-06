"""全專案檢查（網頁左側的背景掃描）。

    python tests/test_scan.py

不需要真實專案，也不會碰使用者的 ~/.csdview_scan（CSDVIEW_SCAN_DIR 導到暫存目錄）。
驗的是「不能拖慢電腦」的那幾條規則與增量快取，而不只是結果對不對。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

os.environ["CSDVIEW_SCAN_DIR"] = tempfile.mkdtemp(prefix="csdview_scan_cache_")

from csdview import scan as scan_mod                           # noqa: E402
from csdview.server import ViewerState                         # noqa: E402
from test_inspector import build_project                       # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


def run_until(state: ViewerState, status: str, timeout: float = 60.0, poll: bool = True) -> dict:
    """一直「假裝網頁在看」直到掃描到達某個狀態。"""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = state.scan.poll(0) if poll else {"status": state.scan.status}
        if last["status"] == status:
            return last
        time.sleep(0.05)
    return last


class Counting:
    """包住 state.check，數它被呼叫幾次（增量快取有沒有作用）。"""

    def __init__(self, state: ViewerState, delay: float = 0.0):
        self.calls: list[str] = []
        original = state.check

        def wrapped(full):
            self.calls.append(os.path.basename(full))
            time.sleep(delay)                 # 模擬大檔，讓「途中暫停」來得及發生
            return original(full)

        state.check = wrapped


def main() -> int:
    root = build_project()
    with open(os.path.join(root, "Broken.csd"), "w", encoding="utf-8") as fh:
        fh.write("<GameFile><not closed")

    # ---------------------------------------------------------- 第一趟：全部查
    state = ViewerState(root)
    counter = Counting(state)
    state.scan.start()
    final = run_until(state, "done")
    results = state.scan.results
    check("第一趟跑完", final.get("status") == "done", final)
    check("每個檔都有結果", set(results) == {"Main.csd", "Inner.csd", "Advisory.csd", "Broken.csd"},
          sorted(results))
    check("Main.csd：有錯誤（缺圖）", results.get("Main.csd", {}).get("e", 0) > 0, results.get("Main.csd"))
    check("Advisory.csd：只有警告（九宮格被擠扁）",
          results.get("Advisory.csd", {}).get("e") == 0 and results["Advisory.csd"].get("w", 0) > 0,
          results.get("Advisory.csd"))
    check("Inner.csd：沒有問題", results.get("Inner.csd", {}).get("e") == 0
          and results["Inner.csd"].get("w") == 0, results.get("Inner.csd"))
    check("壞檔記成錯誤，不會讓整趟停下來",
          results.get("Broken.csd", {}).get("error") and results["Broken.csd"].get("e") == 1,
          results.get("Broken.csd"))
    cache = state.scan._cache_path
    check("結果存到磁碟", os.path.isfile(cache))
    since = final["seq"]
    check("poll(since) 只回之後有變的", state.scan.poll(since)["results"] == {})

    # ---------------------------------------------------------- 第二趟：沒改過的直接沿用
    state2 = ViewerState(root)
    counter2 = Counting(state2)
    check("新開的伺服器讀得到上次的結果", set(state2.scan.results) == set(results))
    state2.scan.start()
    run_until(state2, "done")
    check("沒改過的檔不重查", counter2.calls == [], counter2.calls)

    time.sleep(0.02)
    with open(os.path.join(root, "Inner.csd"), "a", encoding="utf-8") as fh:
        fh.write("\n")
    state2.scan.start()
    run_until(state2, "done")
    check("改過的檔才重查", counter2.calls == ["Inner.csd"], counter2.calls)

    state2.scan.start(force=True)
    run_until(state2, "done")
    check("全部重查：每個檔都再查一次", len(counter2.calls) == 1 + 4, counter2.calls)

    # ---------------------------------------------------------- 檢查規則改版：舊結果作廢
    with open(cache, encoding="utf-8") as fh:
        data = json.load(fh)
    data["version"] = scan_mod.CHECK_VERSION - 1
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    check("規則版本不同的快取不採用", ViewerState(root).scan.results == {})

    # ---------------------------------------------------------- 不拖慢電腦的規則
    state3 = ViewerState(root)
    Counting(state3)
    state3.scan.pause(True)
    state3.scan.start(force=True)
    state3.scan.pause(True)
    time.sleep(0.6)
    snap = state3.scan.poll(0)
    check("按了暫停就不前進", snap["status"] == "paused" and snap["done"] == 0, snap)
    state3.scan.pause(False)
    check("按繼續就接著跑完", run_until(state3, "done").get("status") == "done")

    state4 = ViewerState(root)
    Counting(state4, delay=0.25)
    old_pause = scan_mod.PAUSE_AFTER
    scan_mod.PAUSE_AFTER = 0.3
    try:
        state4.scan.start(force=True)
        time.sleep(1.2)                       # 期間沒有人來 poll（網頁關了或切到背景）
        status = state4.scan.status
        done = state4.scan.done
        check("網頁沒在看就自動暫停", status == "paused" and done < 4, (status, done))
    finally:
        scan_mod.PAUSE_AFTER = old_pause
    check("網頁回來就繼續", run_until(state4, "done").get("status") == "done")

    state5 = ViewerState(root)
    Counting(state5)
    with state5.interactive():                # 使用者正在等某一格算完
        state5.scan.start(force=True)
        time.sleep(0.6)
        for _ in range(10):
            state5.scan.poll(0)
            time.sleep(0.05)
        check("使用者在看畫面時先讓", state5.scan.done == 0, state5.scan.done)
    check("畫面算完就接著跑", run_until(state5, "done").get("status") == "done")

    # ---------------------------------------------------------- 單檔檢查也會更新列表
    state6 = ViewerState(root)
    state6.scan.results.clear()
    state6.check(os.path.join(root, "Advisory.csd"))
    check("打開單一檔時的檢查結果同步到列表", "Advisory.csd" in state6.scan.results)

    print()
    print(f"通過 {len(PASS)}，失敗 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
