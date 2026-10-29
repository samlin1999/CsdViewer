"""並行渲染的競態測試。

批次指令會用執行緒池平行渲染，網頁伺服器也會跨請求共用同一個 Renderer。
只要有任何一個快取沒被正確保護，畫面就會偶爾錯掉——而且不會有錯誤訊息，
只是某一格默默畫成別張圖。這種 bug 一定要靠壓力測試才抓得到。

執行：
    python tests/test_concurrency.py <cocosstudio 資料夾>

不給資料夾就用內建的合成測試檔。離開碼 0 代表沒有競態。
"""
from __future__ import annotations

import ast
import concurrent.futures as futures
import glob
import hashlib
import inspect
import os
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csdview import library_for, parse                        # noqa: E402
from csdview.render import Renderer, RenderOptions            # noqa: E402

FRAMES = 10
ROUNDS = 6
WORKERS = 8

# 九宮格 + 文字是最容易出事的組合：兩者都有衍生快取，
# 而且結果是共用的 PIL 物件
_SYNTHETIC = """<GameFile>
  <PropertyGroup Name="Race" Type="Layer" Version="3.10.0.0" />
  <Content ctype="GameProjectContent"><Content>
    <Animation Duration="30" Speed="1.0000" />
    <ObjectData Name="Layer" Tag="1" ctype="GameLayerObjectData">
      <Size X="400" Y="200" />
      <Children>
        <AbstractNodeData Name="t1" Tag="2" FontSize="20" LabelText="測試文字 ABC"
                          ctype="TextObjectData">
          <Size X="160" Y="24" /><Position X="20" Y="150" />
          <CColor A="255" R="255" G="220" B="120" />
        </AbstractNodeData>
        <AbstractNodeData Name="t2" Tag="3" FontSize="16" LabelText="第二段文字"
                          ctype="TextObjectData">
          <Size X="120" Y="20" /><Position X="20" Y="110" />
          <CColor A="255" R="120" G="220" B="255" />
        </AbstractNodeData>
      </Children>
    </ObjectData>
  </Content></Content>
</GameFile>
"""


def digest(image) -> str:
    return hashlib.md5(image.tobytes()).hexdigest()


def scale9_uses_id() -> bool:
    """快取鍵若呼叫內建 id()，就是歷史 bug 還在。docstring 裡提到 id 不算。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Renderer._scale9)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "id":
            return True
    return False


def check_scale9_keys() -> tuple[bool, str]:
    if scale9_uses_id():
        return False, "_scale9 的快取鍵仍呼叫 id()"
    from csdview.assets import AssetLibrary
    from csdview.model import Node
    from PIL import Image

    tmp = tempfile.mkdtemp()
    renderer = Renderer(AssetLibrary(tmp), RenderOptions())
    node = Node(width=20, height=20, scale9=True,
                cap_left=2, cap_right=2, cap_top=2, cap_bottom=2)
    red = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    green = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
    a = renderer._scale9(red, node, "")
    b = renderer._scale9(green, node, "")
    if digest(a) == digest(b):
        return False, "空 source_key 時兩張不同圖的九宮格結果相同"
    for key in renderer._scale9_cache:
        ident = key[0]
        if isinstance(ident, str) and ident.startswith("#") and ident[1:].isdigit():
            return False, f"快取鍵仍長得像 id()：{ident}"
    return True, ""


def pick_files(root: str, limit: int = 8) -> list[str]:
    """挑九宮格與文字都多的檔案。"""
    out = []
    for path in glob.glob(os.path.join(root, "**", "*.csd"), recursive=True):
        try:
            doc = parse(path)
        except Exception:
            continue
        scale9 = sum(1 for n in doc.root.walk() if n.scale9)
        texts = sum(1 for n in doc.root.walk() if n.ctype.startswith("Text") and n.text)
        if scale9 >= 3 and texts >= 3:
            out.append(path)
        if len(out) >= limit:
            break
    return out


def check(path: str) -> tuple[bool, str]:
    doc = parse(path)
    frames = [float(f) for f in range(FRAMES)]

    # 基準：每格各用一個全新的 Renderer，完全沒有共用
    expected = [digest(Renderer(library_for(path), RenderOptions()).render(doc, f).image)
                for f in frames]

    for attempt in range(ROUNDS):
        # 冷啟動很重要：快取都暖好之後就不容易踩到競態了
        shared = Renderer(library_for(path), RenderOptions())
        with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            got = [digest(r.image)
                   for r in pool.map(lambda f: shared.render(doc, f), frames)]
        if got != expected:
            bad = sum(1 for a, b in zip(expected, got) if a != b)
            return False, f"第 {attempt + 1} 輪有 {bad}/{len(frames)} 格與序列結果不同"
    return True, ""


def main(argv: list[str]) -> int:
    ok, detail = check_scale9_keys()
    print(f"  {'PASS' if ok else 'FAIL'}  _scale9 快取鍵不用 id()"
          + (f"   {detail}" if detail else ""))
    if not ok:
        return 1

    root = argv[1] if len(argv) > 1 else ""
    if root and os.path.isdir(root):
        targets = pick_files(root)
        if not targets:
            print(f"在 {root} 找不到適合的測試檔（需要九宮格與文字都夠多）")
            return 2
    else:
        tmp = tempfile.mkdtemp()
        targets = [os.path.join(tmp, "Race.csd")]
        with open(targets[0], "w", encoding="utf-8") as fh:
            fh.write(_SYNTHETIC)
        print("未指定資料夾，改用合成測試檔（涵蓋範圍較小）")

    print(f"測試 {len(targets)} 個檔案："
          f"各 {FRAMES} 格 × {ROUNDS} 輪 × {WORKERS} 執行緒")
    failed = 0
    for path in targets:
        ok, detail = check(path)
        print(f"  {'PASS' if ok else 'FAIL'}  {os.path.basename(path)[:42]}"
              + (f"   {detail}" if detail else ""))
        failed += not ok

    print()
    if failed:
        print(f"發現競態：{failed} 個檔案的平行結果與序列不符")
        return 1
    print("平行與序列結果完全一致，未發現競態")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
