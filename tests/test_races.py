"""不必真實專案就能跑的競態／效能回歸。

    python tests/test_races.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csdview.assets import AssetLibrary                        # noqa: E402
from csdview.cache import memoize                              # noqa: E402
from csdview.parser import parse, parse_bytes                  # noqa: E402
from csdview.render import Renderer, RenderOptions             # noqa: E402
from csdview.server import ViewerState                         # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


_LAYER = """<GameFile>
  <PropertyGroup Name="Race" Type="Layer" Version="3.10.0.0" />
  <Content ctype="GameProjectContent"><Content>
    <Animation Duration="0" Speed="1.0000" />
    <ObjectData Name="Layer" Tag="1" ctype="GameLayerObjectData">
      <Size X="220" Y="240" />
      <Children>
        <AbstractNodeData Name="panel" Tag="2" ctype="PanelObjectData">
          <Size X="100" Y="80" /><Position X="10" Y="10" />
        </AbstractNodeData>
      </Children>
    </ObjectData>
  </Content></Content>
</GameFile>
"""


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="csdview_races_")
    path = os.path.join(tmp, "Race.csd")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_LAYER)

    # parse_bytes 與 parse 結果一致
    with open(path, "rb") as fh:
        data = fh.read()
    a, b = parse(path), parse_bytes(data, path)
    check("parse_bytes 根節點名稱相同", a.root.name == b.root.name)
    check("parse_bytes 尺寸相同", (a.root.width, a.root.height) == (b.root.width, b.root.height))

    lib = AssetLibrary(tmp)
    drawn = Renderer(lib, RenderOptions()).render(a)
    layout = Renderer(lib, RenderOptions(layout_only=True)).render(a)
    check("layout_only 的畫布尺寸跟真的畫出來一樣",
          layout.canvas_size == drawn.image.size,
          f"layout={layout.canvas_size} drawn={drawn.image.size}")
    check("layout_only 不配置真正的像素畫布", layout.image.size == (1, 1))
    check("layout_only 仍有節點樹", layout.root is not None and layout.root.name == "Layer")
    check("layout_only 子節點還在",
          bool(layout.root and layout.root.children),
          "children=%s" % (len(layout.root.children) if layout.root else None))

    # 檔案清單快取：TTL 內不重掃，fresh 會重掃
    extra = os.path.join(tmp, "NewOne.csd")
    with open(extra, "w", encoding="utf-8") as fh:
        fh.write(_LAYER)
    state = ViewerState(tmp)
    first = state.list_files()
    check("第一次列出兩個檔", len(first) == 2, f"{len(first)}")
    newer = os.path.join(tmp, "Later.csd")
    with open(newer, "w", encoding="utf-8") as fh:
        fh.write(_LAYER)
    cached = state.list_files()
    check("TTL 內沿用快取、看不到新檔", len(cached) == 2, f"{len(cached)}")
    fresh = state.list_files(fresh=True)
    check("fresh=True 看得到新檔", len(fresh) == 3, f"{len(fresh)}")

    # 文件快取有上限
    state._doc_limit = 3
    for i in range(6):
        p = os.path.join(tmp, f"doc{i}.csd")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(_LAYER)
        state.document(p)
    check("文件快取不超過上限", len(state._docs) <= 3, f"{len(state._docs)}")

    cache: dict = {}
    lock = threading.Lock()
    for i in range(10):
        memoize(cache, i, lock, lambda i=i: i, limit=4)
    check("memoize limit 會把快取清回上限內", len(cache) <= 4, f"{len(cache)}")

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
