"""端到端驗證 AI agent 的操作流程。

個別指令正確不代表流程能串起來——agent 是拿前一步的 JSON 去驅動下一步的。
這支測試完整走一次文件（AGENT_GUIDE.md／dropin/CLAUDE.md）描述的移植驗證流程，
每一步只准使用前一步輸出裡承諾的欄位，不得硬編任何檔案知識。

執行：
    python tests/test_agent_flow.py <cocosstudio 資料夾> [檔案數]

離開碼 0 代表流程完整可用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw        # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed.append(name)
        print(f"    {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
        return ok


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "csdview"] + args,
                          capture_output=True, text=True, encoding="utf-8")


def jrun(args: list[str]) -> tuple[dict | None, subprocess.CompletedProcess]:
    proc = run(args + ["--json"])
    try:
        return json.loads(proc.stdout), proc
    except Exception:
        return None, proc


def check_file(path: str, ck: Checker) -> None:
    """對單一 .csd 走完整流程。"""
    out = tempfile.mkdtemp()

    # 1. info：取得動畫、畫布、引用
    info, proc = jrun(["info", path])
    if not ck("info 回傳合法 JSON", info is not None and proc.returncode == 0):
        return
    for field in ("animations", "references", "canvasSize", "duration", "nodeTypes"):
        ck(f"info 有 {field}", field in info)
    if not info["animations"]:
        print("      沒有動畫片段，略過後續步驟")
        return
    anim = info["animations"][0]
    rng = (anim["start"], anim["end"])

    # 2. doctor：資源與舊資源
    doc, _ = jrun(["doctor", path])
    ck("doctor 有 missingAssets / staleLayout / ok",
       all(k in doc for k in ("missingAssets", "staleLayout", "ok")))

    # 3. render：用 info 給的動畫名產生基準圖
    base = os.path.join(out, "base.png")
    rd, proc = jrun(["render", path, "--anim", anim["name"], "--frame", "0", "-o", base])
    if not ck("render 接受 info 給的動畫名", proc.returncode == 0 and rd is not None):
        return
    ck("JSON 的 image 路徑真的存在", os.path.isfile(rd["image"]))
    ck("圖檔尺寸與 imageSize 相符", list(Image.open(rd["image"]).size) == rd["imageSize"])
    ck("frame 落在 info 給的範圍內", rng[0] <= rd["frame"] <= rng[1],
       f"{rd['frame']} in {rng}")
    ck("有 animationFrame", "animationFrame" in rd)

    # 4. 跨指令的節點識別必須一致
    common = ["--anim", anim["name"], "--frame", "0"]
    spec, _ = jrun(["spec", path] + common)
    tree, _ = jrun(["tree", path] + common)
    paths_spec = [n["path"] for n in spec["nodes"]]
    collected: list[str] = []

    def walk(node):
        collected.append(node["path"])
        for child in node.get("children", []):
            walk(child)

    walk(tree["tree"])
    ck("spec 與 tree 的路徑集合相同", set(paths_spec) == set(collected),
       f"{len(paths_spec)} vs {len(collected)}")
    ck("spec / tree / render 的 designRect 一致",
       spec["designRect"] == tree["designRect"] == rd["designRect"])

    # 5. designRect：文件說可能是 null，取值前必須先判斷
    ck("designRect 是 null 或四元素陣列",
       spec["designRect"] is None or len(spec["designRect"]) == 4,
       f"canvasSize={info['canvasSize']}")
    if spec["designRect"] is None:
        ck("designRect 為 null 時 canvasSize 確實是 [0,0]",
           info["canvasSize"] == [0, 0])
    else:
        design, _ = jrun(["spec", path] + common + ["--no-full-bounds"])
        dx, dy = spec["designRect"][0], spec["designRect"][1]
        by_path: dict[str, dict] = {}
        for node in design["nodes"]:
            by_path.setdefault(node["path"], node)
        worst = 0.0
        for node in spec["nodes"]:
            other = by_path.get(node["path"])
            if other is None:
                continue
            worst = max(worst,
                        abs((node["screenRect"][0] - dx) - other["screenRect"][0]),
                        abs((node["screenRect"][1] - dy) - other["screenRect"][1]))
        ck("screenRect 減 designRect 等於設計座標", worst < 0.15, f"最大偏差 {worst:.2f} px")

    # 6. find：用 spec 給的節點名要找得回來
    named = [p for p in paths_spec if "/" in p]
    if named:
        leaf = named[len(named) // 2].split("/")[-1]
        found, _ = jrun(["find", path] + common + ["--name", leaf])
        ck("用 spec 的節點名 find 得到", found["count"] > 0, f"{leaf} -> {found['count']}")
        ck("find 的路徑存在於 spec",
           any(m["path"] in paths_spec for m in found["matches"]))

    # 7. compare：相同→match/0，塗改→different/1，且要能定位到節點
    ref = os.path.join(out, "ref.png")
    run(["render", path] + common + ["--bg", "#000000", "--no-guide", "-o", ref])
    same, proc = jrun(["compare", path, ref] + common + ["--no-diff-image"])
    ck("相同圖 → match 且離開碼 0",
       same["verdict"] == "match" and proc.returncode == 0, same["verdict"])

    broken = Image.open(ref).convert("RGBA")
    ImageDraw.Draw(broken).rectangle(
        [0, 0, broken.size[0] // 3, broken.size[1]], fill=(255, 0, 0, 255))
    broken_path = os.path.join(out, "broken.png")
    broken.save(broken_path)
    diff, proc = jrun(["compare", path, broken_path] + common + ["--no-diff-image"])
    ck("大幅不同 → different 且離開碼 1",
       diff["verdict"] == "different" and proc.returncode == 1,
       f"{diff['verdict']} rc={proc.returncode}")
    ck("有 afterShiftCompensation 可分辨位移與畫錯",
       "afterShiftCompensation" in diff)

    worst_paths = [w["path"] for w in diff["worstRegions"]]
    if worst_paths:
        ck("worstRegions 的 path 查得到規格（存在於同一份 spec）",
           all(p in paths_spec for p in worst_paths), worst_paths[0])
        # 8. 用 compare 指出的節點做隔離檢視
        hidden, proc = jrun(["render", path] + common
                            + ["--hide", worst_paths[0], "-o", os.path.join(out, "h.png")])
        ck("--hide 接受 compare 給的路徑並生效",
           proc.returncode == 0 and hidden["visibleNodes"] < rd["visibleNodes"],
           f"{rd['visibleNodes']} -> {hidden['visibleNodes']}")

    # 9. sheet：整段動畫總覽
    sheet, proc = jrun(["sheet", path, "--anim", anim["name"], "--count", "6",
                        "-o", os.path.join(out, "sheet.png")])
    ck("sheet 的取樣影格都落在該動畫範圍內",
       proc.returncode == 0 and all(rng[0] <= f <= rng[1] for f in sheet["frames"]),
       f"{sheet['frames']} in {rng}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not os.path.isdir(argv[1]):
        print("用法: python tests/test_agent_flow.py <cocosstudio 資料夾> [檔案數]")
        return 2
    root, limit = argv[1], int(argv[2]) if len(argv) > 2 else 3

    listing, _ = jrun(["list", root, "--detail"])
    if not listing or not listing["files"]:
        print(f"在 {root} 找不到 .csd")
        return 2

    # 刻意同時挑「有固定畫布」與「沒有固定畫布」的檔案：
    # 後者佔六成左右，designRect 會是 null，是最容易被漏掉的情況
    with_canvas, without = [], []
    for entry in listing["files"]:
        if not entry.get("animations"):
            continue
        size = entry.get("size") or [0, 0]
        (with_canvas if size[0] > 0 else without).append(entry["file"])
        if len(with_canvas) >= limit and len(without) >= limit:
            break

    targets = with_canvas[:limit] + without[:limit]
    print(f"流程測試：{len(with_canvas[:limit])} 個有固定畫布、"
          f"{len(without[:limit])} 個無固定畫布")

    ck = Checker()
    for path in targets:
        print(f"\n  --- {os.path.basename(path)} ---")
        check_file(path, ck)

    print(f"\n{ck.passed}/{ck.passed + len(ck.failed)} 通過")
    if ck.failed:
        print("失敗項目：")
        for name in ck.failed:
            print(f"   {name}")
        return 1
    print("agent 流程完整可用：每一步的輸出都足以驅動下一步")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
