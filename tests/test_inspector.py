"""網頁介面的屬性面板、檢查、預抓分道與裁切暫存層。

    python tests/test_inspector.py

不需要真實專案：在暫存目錄自己搭一個小專案（缺圖、缺按下圖、同名兄弟節點、
動畫、巢狀 .csd、過期的百分比版面、裁切面板），起一個真的 HTTP 伺服器打 API。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                          # noqa: E402

from csdview import parser, server as S                        # noqa: E402
from csdview.assets import library_for                         # noqa: E402
from csdview.cli import diagnose                               # noqa: E402
from csdview.render import Renderer, RenderOptions             # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'  ok  ' if condition else ' FAIL '}] {name}"
          + (f"  — {detail}" if detail and not condition else ""))


INNER = """<GameFile>
  <PropertyGroup Name="Inner" Type="Node" Version="3.10.0.0" />
  <Content ctype="GameProjectContent"><Content>
    <Animation Duration="0" Speed="1.0000" />
    <ObjectData Name="InnerRoot" Tag="50" ctype="GameNodeObjectData">
      <Size X="0" Y="0" />
      <Children>
        <AbstractNodeData Name="innerSprite" ActionTag="900" Tag="51" ctype="SpriteObjectData">
          <Size X="20" Y="20" /><Position X="0" Y="0" /><AnchorPoint ScaleX="0.5" ScaleY="0.5" />
          <FileData Type="Normal" Path="red.png" Plist="" />
        </AbstractNodeData>
      </Children>
    </ObjectData>
  </Content></Content>
</GameFile>
"""

MAIN = """<GameFile>
  <PropertyGroup Name="Main" Type="Layer" Version="3.10.0.0" />
  <Content ctype="GameProjectContent"><Content>
    <Animation Duration="20" Speed="1.0000">
      <Timeline ActionTag="101" Property="Position">
        <PointFrame FrameIndex="0" X="40" Y="40"><EasingData Type="0" /></PointFrame>
        <PointFrame FrameIndex="20" X="80" Y="40"><EasingData Type="0" /></PointFrame>
      </Timeline>
    </Animation>
    <ObjectData Name="Layer" Tag="1" ctype="GameLayerObjectData">
      <Size X="200" Y="160" />
      <Children>
        <AbstractNodeData Name="mover" ActionTag="101" Tag="2" ctype="SpriteObjectData">
          <Size X="20" Y="20" /><Position X="40" Y="40" /><AnchorPoint ScaleX="0.5" ScaleY="0.5" />
          <FileData Type="Normal" Path="red.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="dup" ActionTag="102" Tag="3" ctype="SpriteObjectData">
          <Size X="10" Y="10" /><Position X="10" Y="140" />
          <FileData Type="Normal" Path="red.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="dup" ActionTag="103" Tag="4" ctype="SpriteObjectData">
          <Size X="10" Y="10" /><Position X="150" Y="140" />
          <FileData Type="Normal" Path="red.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="lost" ActionTag="104" Tag="5" ctype="SpriteObjectData">
          <Size X="10" Y="10" /><Position X="100" Y="100" />
          <FileData Type="Normal" Path="nowhere/missing.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="btn" ActionTag="105" Tag="6" ctype="ButtonObjectData" Scale9Enable="False">
          <Size X="30" Y="20" /><Position X="150" Y="60" />
          <NormalFileData Type="Normal" Path="red.png" Plist="" />
          <PressedFileData Type="Normal" Path="nowhere/pressed.png" Plist="" />
          <DisabledFileData Type="Default" Path="Default/Button_Disable.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="stale" ActionTag="106" Tag="7" ctype="PanelObjectData"
                          PercentWidthEnable="True" ComboBoxIndex="1">
          <Size X="50" Y="10" /><Position X="0" Y="0" />
          <PreSize X="0.5" Y="0.0625" />
          <SingleColor A="255" R="0" G="0" B="255" />
        </AbstractNodeData>
        <AbstractNodeData Name="nest" ActionTag="107" Tag="8" ctype="ProjectNodeObjectData">
          <Size X="0" Y="0" /><Position X="120" Y="120" />
          <FileData Type="Normal" Path="Inner.csd" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="clipper" ActionTag="108" Tag="9" ctype="PanelObjectData" ClipAble="True">
          <Size X="40" Y="40" /><Position X="20" Y="80" />
          <Children>
            <AbstractNodeData Name="big" ActionTag="109" Tag="10" ctype="SpriteObjectData">
              <Size X="100" Y="100" /><Position X="-30" Y="-30" />
              <FileData Type="Normal" Path="big.png" Plist="" />
            </AbstractNodeData>
          </Children>
        </AbstractNodeData>
        <AbstractNodeData Name="bgPanel" ActionTag="112" Tag="13" ctype="PanelObjectData">
          <Size X="60" Y="40" /><Position X="130" Y="0" />
          <FileData Type="Normal" Path="red.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="stretchView" ActionTag="113" Tag="14" ctype="ImageViewObjectData">
          <Size X="60" Y="40" /><Position X="60" Y="0" />
          <FileData Type="Normal" Path="red.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="s9mismatch" ActionTag="114" Tag="15" ctype="ImageViewObjectData"
            Scale9Enable="True" LeftEage="5" RightEage="5" TopEage="5" BottomEage="5"
            Scale9OriginX="10" Scale9OriginY="10" Scale9Width="10" Scale9Height="10">
          <Size X="60" Y="30" /><Position X="0" Y="120" />
          <FileData Type="Normal" Path="bands.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="s9squeeze" ActionTag="115" Tag="16" ctype="ImageViewObjectData"
            Scale9Enable="True" Scale9OriginX="10" Scale9OriginY="10" Scale9Width="10" Scale9Height="10">
          <Size X="12" Y="30" /><Position X="70" Y="120" />
          <FileData Type="Normal" Path="bands.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="s9zero" ActionTag="116" Tag="17" ctype="ImageViewObjectData"
            Scale9Enable="True" Scale9OriginX="0" Scale9OriginY="0" Scale9Width="0" Scale9Height="0">
          <Size X="60" Y="30" /><Position X="90" Y="120" />
          <FileData Type="Normal" Path="bands.png" Plist="" />
        </AbstractNodeData>
        <AbstractNodeData Name="expired" ActionTag="117" Tag="18" ctype="TextObjectData"
            FontSize="20" LabelText="(已過期)" IsCustomSize="True">
          <Size X="73" Y="30" /><Position X="10" Y="60" />
        </AbstractNodeData>
        <AbstractNodeData Name="offscreen" ActionTag="110" Tag="11" ctype="PanelObjectData" ClipAble="True">
          <Size X="40" Y="40" /><Position X="-5000" Y="-5000" />
          <Children>
            <AbstractNodeData Name="hiddenKid" ActionTag="111" Tag="12" ctype="SpriteObjectData">
              <Size X="20" Y="20" /><Position X="0" Y="0" />
              <FileData Type="Normal" Path="red.png" Plist="" />
            </AbstractNodeData>
          </Children>
        </AbstractNodeData>
      </Children>
    </ObjectData>
  </Content></Content>
</GameFile>
"""


ADVISORY = """<GameFile>
  <PropertyGroup Name="Advisory" Type="Layer" Version="3.10.0.0" />
  <Content ctype="GameProjectContent"><Content>
    <Animation Duration="0" Speed="1.0000" />
    <ObjectData Name="Layer" Tag="1" ctype="GameLayerObjectData">
      <Size X="100" Y="100" />
      <Children>
        <AbstractNodeData Name="squeezed" ActionTag="1" Tag="2" ctype="ImageViewObjectData"
            Scale9Enable="True" Scale9OriginX="10" Scale9OriginY="10" Scale9Width="10" Scale9Height="10">
          <Size X="12" Y="30" /><Position X="10" Y="10" />
          <FileData Type="Normal" Path="bands.png" Plist="" />
        </AbstractNodeData>
      </Children>
    </ObjectData>
  </Content></Content>
</GameFile>
"""


def build_project() -> str:
    root = os.path.join(tempfile.mkdtemp(prefix="csdview_insp_"), "cocosstudio")
    os.makedirs(root)
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(os.path.join(root, "red.png"))
    Image.new("RGBA", (100, 100), (0, 255, 0, 255)).save(os.path.join(root, "big.png"))
    # 30x30 直條紋：左 10 藍、中 10 綠、右 10 紅。九宮格切在哪裡，看顏色就知道
    bands = Image.new("RGBA", (30, 30), (0, 0, 255, 255))
    bands.paste((0, 255, 0, 255), (10, 0, 20, 30))
    bands.paste((255, 0, 0, 255), (20, 0, 30, 30))
    bands.save(os.path.join(root, "bands.png"))
    # 專案外的一張圖：貼圖 API 不能讀到它
    Image.new("RGBA", (4, 4), (1, 2, 3, 255)).save(os.path.join(os.path.dirname(root), "outside.png"))
    for name, text in (("Main.csd", MAIN), ("Inner.csd", INNER), ("Advisory.csd", ADVISORY)):
        with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def flatten(tree: dict) -> list[dict]:
    out, stack = [], [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.get("children", [])))
    return out


def rows_of(detail: dict) -> dict[str, dict]:
    return {f"{s['title']}/{r['k']}": r for s in detail["sections"] for r in s["rows"]}


def main() -> int:
    root = build_project()
    S.Handler.state = state = S.ViewerState(root)
    srv, port = S._bind(8870)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(route: str, timeout: float = 30, **query):
        url = f"http://127.0.0.1:{port}{route}?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get_json(route: str, **query):
        code, body = get(route, **query)
        return code, json.loads(body)

    try:
        # ---------------------------------------------------------- 節點樹的 id
        _c, tree = get_json("/api/tree", file="Main.csd", frame=0, full=1)
        flat = flatten(tree["tree"])
        check("每個節點都有 id，且就是前序編號",
              [n["id"] for n in flat] == list(range(len(flat))))
        dups = [n for n in flat if n["name"] == "dup"]
        check("同名兄弟節點各有自己的 id", len(dups) == 2 and dups[0]["id"] != dups[1]["id"])

        # ---------------------------------------------------------- 屬性面板
        _c, second = get_json("/api/node", file="Main.csd", frame=0, full=1,
                              id=dups[1]["id"], path=dups[1]["path"])
        r = rows_of(second)
        check("用 id 指名：同名的第二個節點拿到的是它自己的位置",
              r.get("位置與大小/位置", {}).get("v") == "150, 140", r.get("位置與大小/位置"))
        _c, stale_id = get_json("/api/node", file="Main.csd", frame=0, full=1,
                                id=99999, path=dups[0]["path"])
        check("id 對不上時退回用路徑找", stale_id.get("id") == dups[0]["id"])
        code, gone = get_json("/api/node", file="Main.csd", frame=0, full=1, id=1, path="Layer/none")
        check("找不到的節點回 404 與說明", code == 404 and "找不到" in gone.get("error", ""))

        mover = next(n for n in flat if n["name"] == "mover")
        _c, moving = get_json("/api/node", file="Main.csd", frame=10, full=1,
                              id=mover["id"], path=mover["path"])
        pos = rows_of(moving).get("位置與大小/位置", {})
        check("動畫改過的值：顯示這一格的，附上 .csd 原值",
              pos.get("v") == "60, 40" and pos.get("anim") and pos.get("base") == "40, 40", pos)
        check("時間軸標記：回傳這個節點的關鍵影格",
              moving.get("keyframes", {}).get("tracks") == [
                  {"prop": "Position", "label": "位置", "frames": [0, 20]}], moving.get("keyframes"))
        anim_rows = [s for s in moving["sections"] if s["title"].startswith("動畫")]
        check("動畫分區列出軌道", bool(anim_rows) and anim_rows[0]["rows"][0]["k"] == "位置")

        btn = next(n for n in flat if n["name"] == "btn")
        _c, button = get_json("/api/node", file="Main.csd", frame=0, full=1,
                              id=btn["id"], path=btn["path"])
        r = rows_of(button)
        check("按鈕：一般狀態圖找得到", r.get("資源/一般狀態圖", {}).get("status") == "ok")
        check("按鈕：按下狀態圖標成找不到", r.get("資源/按下狀態圖", {}).get("status") == "missing")
        check("按鈕：Default 圖標成內建圖", r.get("資源/停用狀態圖", {}).get("status") == "default")
        check("原始設定包含 <PressedFileData>", "PressedFileData" in button["raw"]["elements"])

        inner = next(n for n in flat if n["name"] == "innerSprite")
        _c, nested = get_json("/api/node", file="Main.csd", frame=0, full=1,
                              id=inner["id"], path=inner["path"])
        check("巢狀節點註明定義在哪份 .csd",
              rows_of(nested).get("基本/定義在", {}).get("v") == "Inner.csd")

        stale = next(n for n in flat if n["name"] == "stale")
        _c, staled = get_json("/api/node", file="Main.csd", frame=0, full=1,
                              id=stale["id"], path=stale["path"])
        pre = rows_of(staled).get("百分比版面/PreSize", {})
        check("百分比版面：算出來對不上時標警告", pre.get("status") == "warn", pre)

        hidden = next(n for n in flat if n["name"] == "hiddenKid")
        check("裁切框在畫面外：子節點照樣出現在樹裡", hidden is not None)

        # ---------------------------------------------------------- 檢查
        t0 = time.perf_counter()
        _c, chk = get_json("/api/check", file="Main.csd")
        first_ms = (time.perf_counter() - t0) * 1000
        full = os.path.join(root, "Main.csd")
        payload, _last = diagnose(parser.parse(full),
                                  Renderer(library_for(full), RenderOptions(layout_only=True)))
        kinds = {g["kind"]: len(g["items"]) for g in chk["groups"]}
        check("檢查結果跟 CLI doctor 同一份判斷",
              kinds.get("missingAssets", 0) == len(payload["missingAssets"])
              and kinds.get("staleLayout", 0) == len(payload["staleLayout"]), (kinds, payload))
        check("缺少的資源對回用到它的節點",
              "Layer/lost" in chk["byNode"] and chk["byNode"]["Layer/lost"][0]["level"] == "error",
              chk["byNode"])
        check("按下圖不算進缺少的資源（跟 doctor 一致）",
              "nowhere/pressed.png" not in payload["missingAssets"]
              and all(i["text"] != "nowhere/pressed.png"
                      for g in chk["groups"] for i in g["items"]))
        check("版面過期對到 stale 節點", "Layer/stale" in chk["byNode"])
        check("doctor：九宮格編輯器數值不一致",
              [e["path"] for e in payload["scale9Mismatch"]] == ["Layer/s9mismatch"],
              payload["scale9Mismatch"])
        check("doctor：九宮格節點比兩角小",
              [e["path"] for e in payload["scale9Squeezed"]] == ["Layer/s9squeeze"],
              payload["scale9Squeezed"])
        check("doctor：Panel 底圖沒拉伸（提醒）",
              [e["path"] for e in payload["panelBackground"]] == ["Layer/bgPanel"],
              payload["panelBackground"])
        check("網頁檢查：Panel 底圖算提醒、九宮格算警告",
              any(g["kind"] == "panelBackground" and g["level"] == "info" for g in chk["groups"])
              and any(g["kind"] == "scale9Squeezed" and g["level"] == "warn" for g in chk["groups"]))
        adv_full = os.path.join(root, "Advisory.csd")
        adv, _l = diagnose(parser.parse(adv_full),
                           Renderer(library_for(adv_full), RenderOptions(layout_only=True)))
        check("新的三類不影響 ok（csd MCP 的 agent 靠 ok 判斷有沒有改壞）",
              adv["ok"] is True and adv["advisories"] == 1, adv)
        t0 = time.perf_counter()
        get_json("/api/check", file="Main.csd")
        again_ms = (time.perf_counter() - t0) * 1000
        check("檔案沒改，第二次檢查直接用快取", again_ms < max(50.0, first_ms / 2),
              f"{first_ms:.0f} -> {again_ms:.0f} ms")

        # 屬性面板查過按下圖之後，畫面用的素材庫 missing 不能多出它
        lib = state.library(full)
        check("屬性面板的資源查詢不污染素材庫的 missing", "nowhere/pressed.png" not in lib.missing)

        # ---------------------------------------------------------- 預抓分道
        params = dict(file="Main.csd", frame=3, anim="", v="t", bg="checker",
                      annotate="0", particles="1", full="1", hide="", show="")
        code, png_pf = get("/api/frame", pf="1", **params)
        before = state.cache.stats()[0]
        code2, png = get("/api/frame", **params)
        check("pf=1 不進快取鍵：預抓過的格子直接命中",
              code == code2 == 200 and png == png_pf and state.cache.stats()[0] == before)

        # 兩條預抓把預抓道與兩個渲染名額佔滿：使用者看的那一格仍然要能算
        lane, slots = state.prefetch_lane(), state.render_slot()
        for _ in range(2):
            lane.acquire()
            slots.acquire()
        try:
            t0 = time.perf_counter()
            code, _b = get("/api/frame", timeout=10, **{**params, "frame": "5"})
            check("預抓佔滿時，正在看的那一格不必排隊", code == 200,
                  f"{(time.perf_counter() - t0) * 1000:.0f} ms")
            done = threading.Event()

            def prefetch_one():
                get("/api/frame", timeout=10, pf="1", **{**params, "frame": "6"})
                done.set()

            threading.Thread(target=prefetch_one, daemon=True).start()
            check("預抓道滿了，新的預抓要等", not done.wait(0.8))
        finally:
            for _ in range(2):
                lane.release()
                slots.release()
        check("預抓道空出來就接著算", done.wait(10))

        # ---------------------------------------------------------- 換平台會超框（系統字）
        from csdview.platform_fit import PlatformFonts
        fonts = PlatformFonts.get().coverage()
        expired_issue = [e for e in payload["textOverflowPlatforms"] if e["path"] == "Layer/expired"]
        check("Android 沿用 Windows 的字寬",
              "沿用" in fonts["android"]["note"] or "沒得沿用" in fonts["android"]["note"])
        if fonts["windows"]["measured"]:
            got = expired_issue[0]["platforms"] if expired_issue else {}
            check("跨平台：Windows 放得下、iOS 會多一行就報",
                  expired_issue and got.get("ios", {}).get("lines") == 2
                  and "android" not in got and expired_issue[0]["windows"]["lines"] == 1,
                  expired_issue)
            check("跨平台：不算進 ok",
                  "textOverflowPlatforms" in payload and "platformFonts" in payload)
            check("網頁檢查也列出來", "Layer/expired" in chk["byNode"]
                  and any(g["kind"] == "textOverflowPlatforms" for g in chk["groups"]))
            n = next(x for x in flat if x["name"] == "expired")
            _c, d = get_json("/api/node", file="Main.csd", frame=0, full=1, id=n["id"], path=n["path"])
            widths = rows_of(d).get("文字/各平台寬度", {})
            check("屬性面板列出各平台寬度", "iOS" in widths.get("v", "") and widths.get("status") == "warn",
                  widths)
        else:
            print("[ skip ] 這台電腦沒有微軟正黑，跨平台檢查跳過")

        # ---------------------------------------------------------- 九宮格（屬性面板）
        def node_rows(name):
            n = next(x for x in flat if x["name"] == name)
            _c, d = get_json("/api/node", file="Main.csd", frame=0, full=1, id=n["id"], path=n["path"])
            return rows_of(d), d

        r, mismatch = node_rows("s9mismatch")
        check("九宮格：四角照引擎（Scale9*）算",
              r.get("九宮格/四角（不拉伸）", {}).get("v") == "左 10　右 10　上 10　下 10",
              r.get("九宮格/四角（不拉伸）"))
        check("九宮格：LeftEage 那組不一致時警告",
              r.get("九宮格/編輯器的值", {}).get("status") == "warn"
              and "左 5" in r.get("九宮格/編輯器的值", {}).get("v", ""))
        check("九宮格：附預覽（貼圖與切線）",
              r.get("九宮格/切片預覽", {}).get("slice9", {}).get("insets") == [10, 10, 10, 10])
        check("九宮格：畫面切線資料", mismatch.get("slice9", {}).get("left") == 10
              and mismatch["slice9"].get("aligned"), mismatch.get("slice9"))
        r, squeeze = node_rows("s9squeeze")
        check("九宮格：節點比兩角小時警告", r.get("九宮格/注意", {}).get("status") == "warn")
        check("九宮格：切線照引擎縮成一半",
              squeeze.get("slice9", {}).get("left") == 6 and squeeze["slice9"].get("right") == 6,
              squeeze.get("slice9"))
        r, _d = node_rows("bgPanel")
        check("九宮格沒啟用也列出來，並說明 Panel 的規則",
              r.get("九宮格/啟用", {}).get("v") == "否" and "正中央" in r["九宮格/啟用"].get("note", ""))

        code, tex = get("/api/texture", file="Main.csd", type="Normal", path="bands.png")
        check("貼圖 API 回傳專案裡的圖", code == 200 and tex[:4] == b"\x89PNG")
        code, _b = get("/api/texture", file="Main.csd", type="Normal", path="../outside.png")
        check("貼圖 API 讀不到專案外的圖", code == 404)
        code, _b = get("/api/texture", file="Main.csd", type="Normal", path="Main.csd")
        check("貼圖 API 不給非圖檔", code == 404)

        # ---------------------------------------------------------- 重新整理
        get_json("/api/reset", file="Main.csd")
        check("reset 丟掉這個檔案的素材庫", full not in state._libraries)
        check("reset 丟掉這個檔案的 Renderer", all(k[0] != full for k in state._renderers))

        # ---------------------------------------------------------- 素材庫共用
        for bg in ("checker", "#000000", "#ffffff"):
            get("/api/frame", **{**params, "bg": bg, "frame": "7"})
        libs = {id(r.assets) for k, r in state._renderers.items() if k[0] == full}
        check("不同背景色共用同一份素材庫（不重複解碼貼圖）", len(libs) == 1, libs)
    finally:
        srv.shutdown()

    # ---------------------------------------------------------- 裁切暫存層
    full = os.path.join(root, "Main.csd")
    doc = parser.parse(full)
    result = Renderer(library_for(full), RenderOptions()).render(doc, 0.0)
    image = result.image
    clip = next(n for n in result.nodes if n.name == "clipper")
    x, y, w, h = (int(round(v)) for v in clip.rect)
    inside = image.getpixel((x + w // 2, y + h // 2))
    outside = image.getpixel((x + w + 3, y + h // 2))
    check("裁切：框內畫得到子節點", inside[:3] == (0, 255, 0) and inside[3] == 255, inside)
    check("裁切：框外被切掉", outside[3] == 0 or outside[:3] != (0, 255, 0), outside)
    check("裁切框整個在畫面外也不出錯", any(n.name == "hiddenKid" for n in result.nodes))

    # ---------------------------------------------------------- Panel 底圖不拉伸
    # cocos2d-x 的 Layout 沒開九宮格時，底圖以原尺寸擺在框正中央（coinMonopoly/MainView
    # 的頂條就是這樣被畫歪的）；ImageView 則照 Size 拉滿。
    def box_of(name):
        node = next(n for n in result.nodes if n.name == name)
        return tuple(int(round(v)) for v in node.rect)

    px, py, pw, ph = box_of("bgPanel")          # 60x40 的框，20x20 的圖
    corner = image.getpixel((px + 2, py + 2))
    center = image.getpixel((px + pw // 2, py + ph // 2))
    check("Panel 底圖沒開九宮格：框中央畫得到圖", center[:3] == (255, 0, 0) and center[3] == 255, center)
    check("Panel 底圖沒開九宮格：不拉伸，框角落是空的", corner[3] == 0, corner)
    check("Panel 回報的框仍是節點 Size（spec／tree 不變）", (pw, ph) == (60, 40), (pw, ph))
    vx, vy, vw, vh = box_of("stretchView")
    stretched = image.getpixel((vx + 2, vy + 2))
    check("ImageView 照 Size 拉滿", stretched[:3] == (255, 0, 0) and stretched[3] == 255, stretched)

    # ---------------------------------------------------------- 九宮格（畫出來的像素）
    # 條紋貼圖：左 10 藍、中 10 綠、右 10 紅；看節點裡某一欄是什麼顏色，就知道切在哪
    def color_at(name, local_x):
        x, y, _w, h = box_of(name)
        return image.getpixel((x + local_x, y + h // 2))[:3]

    check("九宮格讀 Scale9*（不是 LeftEage）：x=11 已經是中間的綠",
          color_at("s9mismatch", 11) == (0, 255, 0), color_at("s9mismatch", 11))
    check("九宮格右角維持原尺寸：倒數第 5 欄是紅", color_at("s9mismatch", 55) == (255, 0, 0))
    check("Scale9* 全是 0 時用 1/3：x=11 是中間的綠",
          color_at("s9zero", 11) == (0, 255, 0), color_at("s9zero", 11))
    check("節點比兩角窄：左半是左角（藍）", color_at("s9squeeze", 3) == (0, 0, 255),
          color_at("s9squeeze", 3))
    check("節點比兩角窄：右半是右角（紅），兩角各佔一半", color_at("s9squeeze", 8) == (255, 0, 0),
          color_at("s9squeeze", 8))

    print()
    print(f"通過 {len(PASS)}，失敗 {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
