"""網頁介面的專案選單。

    python tests/test_ui_picker.py

`開啟介面.bat` 與 `設定AI助手.bat` 原本各記一份設定（`~/.csdview_ui` 與
`~/.csdview_projects`），互不相干——在設定AI助手選好的專案，開啟介面不知道，
只能靠把資料夾拖到 .bat 上切換。這裡驗的是把登錄檔接進來之後的選單行為。

不需要任何參數，也不會碰到使用者真正的設定檔（都導到暫存目錄）。
"""
from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import csdview.cli as cli                                      # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail else ""))


def setup(count: int, remembered: "int | None" = None):
    """搭一個假的登錄檔與「上次用的」紀錄，回傳 (暫存目錄, 各專案根目錄)。"""
    tmp = tempfile.mkdtemp()
    roots = []
    for i in range(count):
        d = os.path.join(tmp, "proj%d" % i, "cocosstudio")
        os.makedirs(d)
        roots.append(d)
    reg = os.path.join(tmp, "reg.json")
    json.dump([{"directory": os.path.dirname(d), "name": "CLAUDE.md", "csdRoot": d}
               for d in roots], io.open(reg, "w", encoding="utf-8"))
    cli._REGISTRY = reg
    cli._UI_REMEMBER = os.path.join(tmp, "ui")
    if remembered is not None:
        io.open(cli._UI_REMEMBER, "w", encoding="utf-8").write(roots[remembered])
    return tmp, roots


def answering(answers, fn):
    """把 input() 換成固定答案，用來驗互動流程。"""
    it = iter(answers)

    def fake(*_a):
        try:
            return next(it)
        except StopIteration:      # 真實終端的 EOF（Ctrl+D／管線結束）丟的是 EOFError
            raise EOFError

    real = builtins.input
    builtins.input = fake
    try:
        return fn()
    finally:
        builtins.input = real


def main() -> int:
    keep = (cli._REGISTRY, cli._UI_REMEMBER)
    quiet = io.StringIO()
    try:
        tmp, roots = setup(1)
        check("只有一個專案時不會出現選單（候選只有一個）",
              len(cli._ui_candidates()) == 1)
        shutil.rmtree(tmp, ignore_errors=True)

        tmp, roots = setup(3, remembered=1)
        cands = cli._ui_candidates()
        check("上次用的排在第一個", cands[0] == roots[1])
        check("設定過的專案都在候選裡", len(cands) == 3, str(len(cands)))

        real_stdout = sys.stdout
        sys.stdout = quiet          # 選單本身會印一整段，測試輸出不需要它
        try:
            enter = answering([""], lambda: cli._pick_directory(cands))
            three = answering(["3"], lambda: cli._pick_directory(cands))
            pasted = answering([roots[2]], lambda: cli._pick_directory(cands))
            retried = answering(["這不是路徑", ""],
                                lambda: cli._pick_directory(cands))
            interrupted = answering([], lambda: cli._pick_directory(cands))
        finally:
            sys.stdout = real_stdout

        check("Enter 沿用上次那個", enter == cands[0])
        check("輸入編號會切換", three == cands[2])
        check("直接貼資料夾路徑也可以", pasted == roots[2])
        check("亂輸入會重問，不會當掉也不會亂選", retried == cands[0])
        check("中途按 Ctrl+C／EOF 退回上次那個，不會爆掉", interrupted == cands[0])
        shutil.rmtree(tmp, ignore_errors=True)

        tmp, roots = setup(2, remembered=0)
        shutil.rmtree(os.path.dirname(roots[1]), ignore_errors=True)
        check("專案被搬走後不會出現在候選裡", len(cli._ui_candidates()) == 1)
        shutil.rmtree(tmp, ignore_errors=True)

        # ---- 拖新資料夾進去要累積，而不是每次蓋掉上一個 ----------------
        tmp, roots = setup(4)
        io.open(cli._REGISTRY, "w", encoding="utf-8").write("[]")
        io.open(cli._UI_REMEMBER, "w", encoding="utf-8").write("[]")
        for d in roots[:3]:
            cli._remember_dir(d)
        cands = cli._ui_candidates()
        check("開過的資料夾會累積，不會蓋掉上一個", len(cands) == 3, str(len(cands)))
        check("由新到舊排序", cands == [roots[2], roots[1], roots[0]])

        cli._remember_dir(roots[0])
        cands = cli._ui_candidates()
        check("重開舊的不會重複出現，只會被提到最前面",
              len(cands) == 3 and cands[0] == roots[0])

        json.dump([{"directory": os.path.dirname(roots[3]), "name": "CLAUDE.md",
                    "csdRoot": roots[3]}], io.open(cli._REGISTRY, "w", encoding="utf-8"))
        cands = cli._ui_candidates()
        check("設定AI助手的專案也會併進來", roots[3] in cands)
        check("介面自己開過的排在登錄檔的前面", cands[0] == roots[0])

        io.open(cli._UI_REMEMBER, "w", encoding="utf-8").write(roots[1])
        check("舊格式（單獨一行路徑）仍讀得動",
              cli._remembered_dirs() == [roots[1]])

        io.open(cli._UI_REMEMBER, "w", encoding="utf-8").write("[]")
        for i in range(cli._UI_KEEP + 4):
            d = os.path.join(tmp, "m%d" % i, "cocosstudio")
            os.makedirs(d)
            cli._remember_dir(d)
        check("清單有上限，不會無限長大",
              len(json.load(io.open(cli._UI_REMEMBER, encoding="utf-8")))
              == cli._UI_KEEP)
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        cli._REGISTRY, cli._UI_REMEMBER = keep

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
