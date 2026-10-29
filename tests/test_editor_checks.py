"""doctor 裡「csdview 正常、但 Cocos Studio 打不開」那幾項檢查的回歸測試。

    python tests/test_editor_checks.py

自己搭一個最小專案，不依賴任何實際專案有沒有圖集，所以在任何機器上都跑得起來。

驗的是這件事：圖被 .csi 打包進 plist 之後，.csd 必須寫 Type="MarkedSubImage"
並指定 Plist。寫成 Normal 時 csdview 照樣讀得到磁碟上的散圖、渲染完全正常，
Cocos Studio 卻是照資源樹解析，開起來會整片掉圖——這個檢查是唯一驗得出來的。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image                                          # noqa: E402

from csdview import assets                                     # noqa: E402
from csdview.cli import _atlas_mismatch, _zero_size_widgets     # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail else ""))


CSI = """<PlistInfoProjectFile>
  <PropertyGroup Name="pack" Type="Plist" />
  <Content ctype="PlistInfoData">
    <ImageFiles>
      <FilePathData Path="mod/img/packed.png" />
      <FilePathData Path="mod/img/packed two.png" />
    </ImageFiles>
  </Content>
</PlistInfoProjectFile>
"""

CSD = """<GameFile>
  <PropertyGroup Name="{name}" Type="Layer" Version="3.10.0.0" />
  <Content ctype="GameProjectContent">
    <Content>
      <ObjectData Name="Layer" ctype="GameLayerObjectData">
        <Size X="100" Y="100" />
        <Children>
          <AbstractNodeData Name="a" ActionTag="1" ctype="SpriteObjectData">
            <Position X="0" Y="0" />
            <FileData Type="{t1}" Path="mod/img/packed.png" Plist="{p1}" />
          </AbstractNodeData>
          <AbstractNodeData Name="b" ActionTag="2" ctype="ButtonObjectData">
            <Position X="0" Y="0" />
            <NormalFileData Type="{t2}" Path="mod/img/packed two.png" Plist="{p2}" />
            <PressedFileData Type="{t2}" Path="mod/img/packed two.png" Plist="{p2}" />
          </AbstractNodeData>
          <AbstractNodeData Name="c" ActionTag="3" ctype="SpriteObjectData">
            <Position X="0" Y="0" />
            <FileData Type="Normal" Path="mod/img/loose.png" Plist="" />
          </AbstractNodeData>
        </Children>
      </ObjectData>
    </Content>
  </Content>
</GameFile>
"""

PLIST = "mod/img/pack.plist"


def write_csd(root: str, name: str, t1: str, p1: str, t2: str, p2: str) -> str:
    path = os.path.join(root, "mod", name + ".csd")
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        CSD.format(name=name, t1=t1, p1=p1, t2=t2, p2=p2))
    return path


def main() -> int:
    root = tempfile.mkdtemp()
    try:
        img = os.path.join(root, "mod", "img")
        os.makedirs(img)
        for f in ("packed.png", "packed two.png", "loose.png"):
            Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(os.path.join(img, f))
        io.open(os.path.join(img, "pack.csi"), "w", encoding="utf-8",
                newline="\n").write(CSI)

        table = assets.atlas_map(root)
        check("讀得出 .csi 的圖集成員", len(table) == 2, str(sorted(table)))
        check("圖集的 plist 路徑推得對",
              table.get("mod/img/packed.png") == PLIST, str(table.get("mod/img/packed.png")))

        # 1) 全部寫成 Normal —— 就是 Cocos Studio 掉圖的那種檔
        bad = write_csd(root, "bad", "Normal", "", "Normal", "")
        hits = _atlas_mismatch(bad, root)
        paths = {(h["path"], h["element"]) for h in hits}
        check("打包過的圖寫成 Normal 會被抓出來", len(hits) == 3, f"{len(hits)} 處")
        check("連渲染用不到的 PressedFileData 也檢查",
              ("Layer/b", "PressedFileData") in paths, str(sorted(paths)))
        check("沒被打包的散圖不會被誤報",
              all("loose.png" not in h["image"] for h in hits))
        check("有回報應該指向哪個 plist",
              all(h["expectedPlist"] == PLIST for h in hits))
        check("節點路徑是完整路徑，不是只有根節點",
              ("Layer/a", "FileData") in paths, str(sorted(paths)))

        # 2) 正確寫法 —— 應該完全安靜
        good = write_csd(root, "good", "MarkedSubImage", PLIST, "MarkedSubImage", PLIST)
        check("正確指向圖集時不會誤報", _atlas_mismatch(good, root) == [])

        # 3) 指到別的 plist —— 一樣要抓出來
        wrong = write_csd(root, "wrong", "MarkedSubImage", "mod/img/other.plist",
                          "MarkedSubImage", PLIST)
        hits = _atlas_mismatch(wrong, root)
        check("指到錯的 plist 也會被抓出來", len(hits) == 1, f"{len(hits)} 處")
        check("錯的 plist 有回報現值與應有值",
              bool(hits) and hits[0]["plist"] == "mod/img/other.plist"
              and hits[0]["expectedPlist"] == PLIST)

        # ---- 尺寸 0 的元件（Cocos Studio 不顯示） -------------------------
        sized = os.path.join(root, "mod", "sized.csd")
        io.open(sized, "w", encoding="utf-8", newline=chr(10)).write(
            CSD.format(name="sized", t1="MarkedSubImage", p1=PLIST,
                       t2="MarkedSubImage", p2=PLIST)
            .replace('<Position X="0" Y="0" />',
                     '<Size X="0" Y="0" /><Position X="0" Y="0" />'))
        hits = _zero_size_widgets(sized)
        kinds = {h["type"] for h in hits}
        check("Button 尺寸 0x0 會被抓出來", "Button" in kinds, str(sorted(kinds)))
        check("Sprite 尺寸 0 不算錯（本來就用貼圖原始尺寸）", "Sprite" not in kinds)

        # 單邊 0 是進度條填充的正常寫法，不能報
        half = os.path.join(root, "mod", "half.csd")
        io.open(half, "w", encoding="utf-8", newline=chr(10)).write(
            io.open(sized, encoding="utf-8").read()
            .replace('<Size X="0" Y="0" />', '<Size X="0" Y="36" />'))
        check("單邊為 0 不算錯（進度條由程式撐開寬度）",
              _zero_size_widgets(half) == [], str(_zero_size_widgets(half)))

        # 有尺寸就完全安靜
        ok_file = os.path.join(root, "mod", "ok.csd")
        io.open(ok_file, "w", encoding="utf-8", newline=chr(10)).write(
            io.open(sized, encoding="utf-8").read()
            .replace('<Size X="0" Y="0" />', '<Size X="66" Y="66" />'))
        check("尺寸正常時不會誤報", _zero_size_widgets(ok_file) == [])

        # 4) 專案裡沒有 .csi 時，這個檢查要完全不作用（不能亂報）
        plain = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(plain, "mod", "img"))
            for f in ("packed.png", "packed two.png", "loose.png"):
                Image.new("RGBA", (8, 8), (0, 255, 0, 255)).save(
                    os.path.join(plain, "mod", "img", f))
            nocsi = write_csd(plain, "bad", "Normal", "", "Normal", "")
            check("專案沒有圖集定義時完全不作用",
                  assets.atlas_map(plain) == {} and _atlas_mismatch(nocsi, plain) == [])
        finally:
            shutil.rmtree(plain, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n通過 {len(PASS)}，失敗 {len(FAIL)}")
    if FAIL:
        print("失敗項目：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
