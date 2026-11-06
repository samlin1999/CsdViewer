"""網頁屬性面板的內容：把一個節點在某一格的狀態整理成「分區 + 一列一項」。

伺服器只回結構化的列（標籤、值、狀態），前端照樣排版，不必懂 .csd。
每一列的值都寫成人看得懂的字（「靠左」「加法混色」「找不到」），
被動畫改過的值同時附上 .csd 裡原本寫的，一眼分得出「檔案寫的」跟「這一格實際的」。

另外整理「檢查」的結果：doctor 的每一類問題攤平成一條條，並附上是哪些節點，
前端點一下就能跳到那個節點。
"""
from __future__ import annotations

import os

from . import anim
from .assets import AssetLibrary
from .model import BlendFunc, Color, Document, FileRef
from .render import NodeReport

# ---------------------------------------------------------------- 對照表

TYPE_NAMES = {
    "SingleNode": "節點", "Node": "節點", "GameNode": "根節點", "GameLayer": "根圖層",
    "Layer": "圖層", "Sprite": "精靈圖", "ImageView": "圖片", "Panel": "面板",
    "Button": "按鈕", "CheckBox": "核取方塊", "Text": "文字", "TextBMFont": "點陣字",
    "TextField": "輸入框", "TextAtlas": "數字圖字", "ProjectNode": "巢狀 CSD",
    "ScrollView": "捲動區", "ListView": "清單", "PageView": "分頁",
    "LoadingBar": "進度條", "Slider": "滑桿", "Particle": "粒子",
}

# 有 Path 屬性的設定元素 -> 在面板上叫什麼
RESOURCE_LABELS = {
    "FileData": "圖片",
    "NormalFileData": "一般狀態圖",
    "PressedFileData": "按下狀態圖",
    "DisabledFileData": "停用狀態圖",
    "NormalBackFileData": "底圖（一般）",
    "PressedBackFileData": "底圖（按下）",
    "DisableBackFileData": "底圖（停用）",
    "NodeNormalFileData": "勾選圖（一般）",
    "NodeDisableFileData": "勾選圖（停用）",
    "ImageFileData": "進度條圖",
    "BackGroundData": "滑軌底圖",
    "ProgressBarData": "進度圖",
    "BallNormalData": "滑塊（一般）",
    "BallPressedData": "滑塊（按下）",
    "BallDisabledData": "滑塊（停用）",
    "FontResource": "字型檔",
    "LabelBMFontFile_CNB": "點陣字型",
    "LabelAtlasFileImage_CNB": "數字圖字圖",
}

PROP_LABELS = {
    "Position": "位置", "Scale": "縮放", "RotationSkew": "旋轉", "AnchorPoint": "錨點",
    "Alpha": "透明度", "CColor": "顏色", "VisibleForFrame": "可見", "FileData": "換圖",
    "BlendFunc": "混色", "FrameEvent": "事件", "ActionValue": "巢狀動畫",
}

_H_ALIGN = {"HT_Left": "靠左", "HT_Center": "置中", "HT_Right": "靠右", "": "靠左（預設）"}
_V_ALIGN = {"VT_Top": "靠上", "VT_Center": "置中", "VT_Bottom": "靠下", "": "靠上（預設）"}
_EDGE = {"LeftEdge": "貼左", "RightEdge": "貼右", "BothEdge": "左右都貼",
         "TopEdge": "貼上", "BottomEdge": "貼下"}

_TEXT_TYPES = {"TextObjectData", "TextFieldObjectData"}
_CONTAINERS = {"PanelObjectData", "ScrollViewObjectData", "ListViewObjectData",
               "PageViewObjectData"}

# 這些在專屬分區已經講過，「原始設定」就不再重複列——那一區是給「還有什麼」用的
_SHOWN_ATTRS = {"Name", "ctype", "ActionTag", "Tag"}


# ---------------------------------------------------------------- 格式化

def num(v: float) -> str:
    """去掉無意義的小數：12.0 -> 12、0.5 -> 0.5、1/3 -> 0.33。"""
    v = round(float(v), 2)
    if v == 0:
        return "0"                       # 不要 -0
    return f"{v:g}" if abs(v) < 1e6 else f"{v:.0f}"


def pair(p, sep: str = ", ") -> str:
    return f"{num(p[0])}{sep}{num(p[1])}"


def hex_color(c: Color | None) -> str:
    if c is None:
        return ""
    return f"#{c.r:02X}{c.g:02X}{c.b:02X}"


def blend_text(b: BlendFunc | None) -> str:
    if b is None or (b.src, b.dst) == (770, 771):
        return "一般"
    if b.is_additive:
        return f"加法混色（發光） Src={b.src} Dst={b.dst}"
    return f"自訂 Src={b.src} Dst={b.dst}"


def value_text(v) -> str:
    """動畫關鍵影格的值轉成字。"""
    if isinstance(v, bool):
        return "顯示" if v else "隱藏"
    if isinstance(v, (int, float)):
        return num(v)
    if isinstance(v, tuple) and len(v) == 2:
        return pair(v)
    if isinstance(v, Color):
        return hex_color(v) + ("" if v.a == 255 else f" α{v.a}")
    if isinstance(v, FileRef):
        return v.path or "(空)"
    if isinstance(v, BlendFunc):
        return blend_text(v)
    if isinstance(v, dict):
        kind = {"SingleFrame": "停在單格", "LoopAction": "循環播放",
                "NoLoopAction": "播一次"}.get(v.get("type", ""), v.get("type", ""))
        name = v.get("animation") or "整條時間軸"
        return f"{name} · {kind}" + (f" 第 {v.get('frame')} 格" if v.get("type") == "SingleFrame" else "")
    return str(v) if v not in (None, "") else "(空)"


def row(k: str, v: str, **extra) -> dict:
    out = {"k": k, "v": v}
    out.update({key: val for key, val in extra.items() if val not in (None, "", False)})
    return out


def changed_row(k: str, now: str, base: str, animated: bool) -> dict:
    """被動畫改掉的值：顯示這一格的，並附上 .csd 原本寫的。"""
    if animated and now != base:
        return row(k, now, anim=True, base=base)
    return row(k, now)


# ---------------------------------------------------------------- 資源

def ref_status(library: AssetLibrary, kind: str, path: str, plist: str) -> tuple[str, str]:
    """(狀態, 說明)。狀態：ok / missing / default / warn。

    刻意不走 library.image()：那會把找不到的記進 missing，污染「檢查」的結果——
    按下圖、停用圖渲染用不到，CLI 的 doctor 也不會把它們算進缺少的資源。
    """
    path = (path or "").replace("\\", "/")
    plist = (plist or "").replace("\\", "/")
    if not path:
        return "", ""
    if kind == "Default":
        return "default", "Cocos Studio 內建圖"
    if library.resolve(path):
        if kind == "PlistSubImage" and plist:
            return "ok", "找得到（散圖）"
        return "ok", "找得到"
    if plist:
        if not library.resolve(plist):
            return "missing", f"找不到圖集 {plist}"
        atlas = library.atlas(plist)
        if atlas is None:
            return "missing", f"圖集 {plist} 讀不出來"
        if path in atlas.frames or os.path.basename(path) in atlas.frames:
            return "ok", "找得到（圖集）"
        return "missing", f"圖集 {plist} 裡沒有這張"
    return "missing", "找不到"


def _font_status(library: AssetLibrary, path: str) -> tuple[str, str]:
    if not path:
        return "", ""
    if library.resolve(path):
        return "ok", "找得到"
    return "warn", "找不到，這裡改用系統字型畫"


# ---------------------------------------------------------------- 節點

def node_details(report: NodeReport, chain: list[NodeReport], library: AssetLibrary,
                 overrides: dict[str, bool], clip: tuple[int, int] | None,
                 root_doc: Document) -> dict:
    """chain 是從根到這個節點的祖先（不含自己），用來說明「為什麼看不到」與版面基準。"""
    node = report.node
    live = report.live or {}
    doc = report.owner
    animated = bool(live.get("animated"))
    short = report.ctype.replace("ObjectData", "")
    sections: list[dict] = []

    # ---- 基本
    basic = [
        row("名稱", report.name or "(無名)"),
        row("類型", f"{TYPE_NAMES.get(short, short)}（{short}）"),
        row("節點路徑", report.path, mono=True),
    ]
    if doc is not None and doc is not root_doc:
        basic.append(row("定義在", os.path.basename(doc.path),
                         note="這個節點在被引用的 .csd 裡，要改得去改那份"))
    basic.append(row("ActionTag", str(report.action_tag), mono=True))
    if node is not None and node.tag:
        basic.append(row("Tag", str(node.tag), mono=True))
    if report.children:
        basic.append(row("子節點", f"{len(report.children)} 個"))
    sections.append({"title": "基本", "rows": basic})

    if node is None:                      # 理論上不會發生，保險
        return {"sections": sections, "raw": {}}

    # ---- 顯示
    vis_rows = []
    csd_visible = node.visible
    vis_rows.append(row(".csd 設定", "顯示" if csd_visible else "隱藏",
                        status="" if csd_visible else "warn"))
    reason = ""
    override = overrides.get(report.path)
    if override is not None:
        reason = "你在階層面板強制" + ("顯示" if override else "隱藏")
    elif not report.visible:
        hidden_parent = next((p for p in reversed(chain) if not p.visible), None)
        if live.get("visible") is False and csd_visible:
            reason = "動畫在這一格把它隱藏"
        elif not csd_visible:
            reason = ".csd 設為隱藏"
        elif hidden_parent is not None:
            reason = f"父層「{hidden_parent.name}」隱藏，所以看不到"
    vis_rows.append(row("這一格", "看得到" if report.visible else "看不到",
                        status="ok" if report.visible else "warn", note=reason))
    if report.visible and report.alpha <= 0.002:
        vis_rows.append(row("注意", "完全透明，看不到", status="warn",
                            note="透明度（含父層累乘）是 0"))
    sections.append({"title": "顯示", "rows": vis_rows})

    # ---- 變換
    tr = []
    base_pos = (node.x, node.y)
    tr.append(changed_row("位置", pair(report.position), pair(base_pos), animated))
    tr.append(row("尺寸", f"{num(node.width)} × {num(node.height)}"))
    if abs(report.size[0] - node.width) > 0.5 or abs(report.size[1] - node.height) > 0.5:
        tr.append(row("實際內容尺寸", f"{num(report.size[0])} × {num(report.size[1])}",
                      note="依貼圖或文字量出來的大小"))
    tr.append(changed_row("錨點", pair(live.get("anchor", (node.anchor_x, node.anchor_y))),
                          pair((node.anchor_x, node.anchor_y)), animated))
    tr.append(changed_row("縮放", pair(live.get("scale", (node.scale_x, node.scale_y))),
                          pair((node.scale_x, node.scale_y)), animated))
    rot = live.get("rotation", (node.rotation_x, node.rotation_y))

    def rot_text(r) -> str:
        return f"{num(r[0])}°" if abs(r[0] - r[1]) < 1e-6 else f"X {num(r[0])}° / Y {num(r[1])}°（skew）"

    tr.append(changed_row("旋轉", rot_text(rot), rot_text((node.rotation_x, node.rotation_y)),
                          animated))
    flips = [name for name, on in (("水平", node.flip_x), ("垂直", node.flip_y)) if on]
    if flips:
        tr.append(row("翻轉", "、".join(flips)))
    x, y, w, h = report.rect
    tr.append(row("畫面上", f"x {num(x)}, y {num(y)}, {num(w)} × {num(h)}",
                  note="渲染圖的像素座標，左上為原點"))
    sections.append({"title": "位置與大小", "rows": tr})

    # ---- 外觀
    look = []
    color = live.get("color") or node.color
    look.append(changed_row("顏色", hex_color(color), hex_color(node.color), animated)
                | {"color": hex_color(color)})
    own_alpha = float(live.get("alpha", node.alpha))
    look.append(changed_row("透明度", f"{num(own_alpha)} / 255（{round(own_alpha / 2.55)}%）",
                            f"{node.alpha} / 255", animated))
    look.append(row("實際透明度", f"{round(report.alpha * 100)}%", note="含父層一路累乘"))
    blend = live.get("blend") if live.get("blend") is not None else node.blend
    look.append(changed_row("混色", blend_text(blend), blend_text(node.blend), animated))
    sections.append({"title": "外觀", "rows": look})

    # ---- 資源
    res = []
    for tag, attrs in node.elements.items():
        path = attrs.get("Path")
        if path is None:
            continue
        label = RESOURCE_LABELS.get(tag, tag)
        if tag == "FileData":
            label = {"ProjectNodeObjectData": "引用的 .csd", "ParticleObjectData": "粒子設定",
                     }.get(node.ctype, "背景圖" if node.ctype in _CONTAINERS else "圖片")
        kind = attrs.get("Type", "Normal")
        plist = attrs.get("Plist", "")
        if not path:
            res.append(row(label, "(沒有設定)", note=tag))
            continue
        if tag == "FontResource":
            status, note = _font_status(library, path)
        else:
            status, note = ref_status(library, kind, path, plist)
        extra = f"{tag} · Type={kind}" + (f" · Plist={plist}" if plist else "")
        res.append(row(label, path.replace("\\", "/"), mono=True, status=status,
                       note=f"{note}　{extra}"))
    live_file = live.get("file")
    if (isinstance(live_file, FileRef) and live_file.path
            and live_file.path != node.file.path):
        status, note = ref_status(library, live_file.type, live_file.path, live_file.plist)
        res.append(row("這一格換成", live_file.path, mono=True, anim=True,
                       status=status, note=note + "（動畫換圖）"))
    if res:
        sections.append({"title": "資源", "rows": res})

    # ---- 文字
    is_text = node.ctype in _TEXT_TYPES
    if is_text or node.ctype in ("TextBMFontObjectData", "ButtonObjectData") or node.text:
        tx = [row("內容", node.text if node.text else "(空白)", pre=True)]
        if node.ctype != "TextBMFontObjectData":
            tx.append(row("字級", num(node.font_size)))
            font = node.font_file.path or node.font_name or "(系統預設)"
            tx.append(row("字型", font, mono=bool(node.font_file.path)))
        if node.text_color is not None and node.ctype == "ButtonObjectData":
            tx.append(row("文字顏色", hex_color(node.text_color), color=hex_color(node.text_color)))
        if is_text:
            tx.append(row("水平對齊", _H_ALIGN.get(node.h_align, node.h_align)))
            tx.append(row("垂直對齊", _V_ALIGN.get(node.v_align, node.v_align)))
            tx.append(row("固定框", "是（IsCustomSize，超出會換行）" if node.custom_size
                          else "否（框跟著文字長）"))
            if node.outline_size:
                tx.append(row("描邊", f"{node.outline_size} px",
                              color=hex_color(node.outline_color)))
            if node.shadow_color is not None:
                tx.append(row("陰影", f"位移 {pair(node.shadow_offset)}",
                              color=hex_color(node.shadow_color)))
        if report.overflow:
            tx.append(row("超出框", f"需要 {pair(report.overflow, ' × ')}", status="warn",
                          note=f"框只有 {pair(report.size, ' × ')}，引擎端同樣會超出"))
        sections.append({"title": "文字", "rows": tx})

    # ---- 九宮格
    if node.scale9:
        sections.append({"title": "九宮格", "rows": [
            row("啟用", "是"),
            row("邊距", f"左 {num(node.cap_left)}　右 {num(node.cap_right)}　"
                        f"上 {num(node.cap_top)}　下 {num(node.cap_bottom)}"),
        ]})

    # ---- 容器
    if node.ctype in _CONTAINERS:
        box = [row("裁切", "是（超出框的子節點會被切掉）" if node.clip else "否")]
        mode = {0: "無", 1: "單色", 2: "漸層"}.get(int(float(node.attrs.get("ComboBoxIndex", 0) or 0)), "無")
        box.append(row("底色", mode))
        if mode == "單色" and node.back_color is not None:
            box.append(row("底色顏色", hex_color(node.back_color), color=hex_color(node.back_color)))
        if mode == "漸層" and node.back_gradient:
            a, b = node.back_gradient
            box.append(row("漸層", f"{hex_color(a)} → {hex_color(b)}　角度 {num(node.color_angle)}°",
                           color=hex_color(a)))
        if mode != "無":
            box.append(row("底色透明度", f"{node.back_color_alpha} / 255"))
        inner = node.elements.get("InnerNodeSize")
        if inner:
            box.append(row("內容大小", f"{inner.get('Width', '?')} × {inner.get('Height', '?')}"))
        direction = node.attrs.get("ScrollDirectionType")
        if direction:
            box.append(row("捲動方向", {"Vertical": "垂直", "Horizontal": "水平",
                                     "Vertical_Horizontal": "雙向"}.get(direction, direction)))
        sections.append({"title": "容器", "rows": box})

    # ---- 進度條／滑桿
    if node.ctype in ("LoadingBarObjectData", "SliderObjectData"):
        bar = [row("進度", f"{num(node.percent)}%")]
        if node.ctype == "LoadingBarObjectData":
            bar.append(row("方向", {"Left_To_Right": "由左往右", "Right_To_Left": "由右往左"}
                           .get(node.progress_dir, node.progress_dir)))
        sections.append({"title": "進度", "rows": bar})

    # ---- 巢狀 CSD
    if node.ctype == "ProjectNodeObjectData":
        nest = [row("內部動畫速度", f"{num(node.inner_speed)}x")]
        if live.get("inner"):
            nest.append(row("這一格播放", value_text(live["inner"]), anim=True))
        if report.document:
            nest.append(row("展開的檔案", report.document, mono=True))
        sections.append({"title": "巢狀 CSD", "rows": nest})

    # ---- 百分比版面
    lay = []
    parent = next((p for p in reversed(chain) if p.owner is doc), None)
    pw, ph = _layout_base(chain, doc)
    if node.percent_size[0] or node.percent_size[1]:
        pre = node.pre_size or (0.0, 0.0)
        lay.append(row("尺寸用百分比", "寬" * node.percent_size[0] + ("、" if all(node.percent_size) else "")
                       + "高" * node.percent_size[1]))
        lay.append(_percent_row("PreSize", pre, node.percent_size, (node.width, node.height), (pw, ph)))
    if node.percent_position[0] or node.percent_position[1]:
        pre = node.pre_position or (0.0, 0.0)
        lay.append(row("位置用百分比", "X" * node.percent_position[0]
                       + ("、" if all(node.percent_position) else "") + "Y" * node.percent_position[1]))
        lay.append(_percent_row("PrePosition", pre, node.percent_position, (node.x, node.y), (pw, ph)))
    for key, label in (("HorizontalEdge", "水平停靠"), ("VerticalEdge", "垂直停靠")):
        if node.attrs.get(key):
            lay.append(row(label, _EDGE.get(node.attrs[key], node.attrs[key])))
    margins = [(label, node.attrs[key]) for key, label in
               (("LeftMargin", "左"), ("RightMargin", "右"), ("TopMargin", "上"), ("BottomMargin", "下"))
               if node.attrs.get(key)]
    if margins and lay:
        lay.append(row("邊距", "　".join(f"{label} {num(float(v))}" for label, v in margins)))
    if lay:
        if parent is not None:
            lay.append(row("基準", f"父層「{parent.name}」 {num(pw)} × {num(ph)}"))
        sections.append({"title": "百分比版面", "rows": lay})

    # ---- 動畫
    if doc is not None and node.action_tag:
        timelines = doc.timelines_by_tag().get(node.action_tag, [])
        frame = float(live.get("frame", 0))
        an = []
        for tl in timelines:
            label = PROP_LABELS.get(tl.prop, tl.prop)
            if tl.prop in ("FrameEvent", "ActionValue"):
                kf, _b, _t = anim._bracket(tl.frames, frame)
                current = value_text(kf.value) if kf.index <= frame else "—"
            else:
                current = value_text(anim.evaluate(tl, frame))
            span = f"{tl.start}–{tl.end}" if tl.start != tl.end else f"{tl.start}"
            unused = (doc is root_doc and clip is not None
                      and (tl.end < clip[0] or tl.start > clip[1]))
            an.append(row(label, current,
                          note=f"{len(tl.frames)} 個關鍵影格，第 {span} 格"
                               + ("（不在目前這段動畫裡）" if unused else ""),
                          dim=unused))
        if an:
            title = "動畫" + (f"（第 {num(frame)} 格）" if doc is root_doc
                            else f"（{os.path.basename(doc.path)} 第 {num(frame)} 格）")
            sections.append({"title": title, "rows": an})

    raw_attrs = {k: v for k, v in node.attrs.items() if k not in _SHOWN_ATTRS}
    return {"sections": sections,
            "raw": {"attrs": raw_attrs, "elements": node.elements}}


def _layout_base(chain: list[NodeReport], doc) -> tuple[float, float]:
    """百分比的基準尺寸，算法同 cli._stale_layout：父層尺寸是 0 就往上找。"""
    same = [p for p in chain if p.owner is doc and p.node is not None]
    w = h = 0.0
    for p in reversed(same):
        if not w and p.node.width:
            w = p.node.width
        if not h and p.node.height:
            h = p.node.height
        if w and h:
            break
    return w, h


def _percent_row(label: str, pre, enabled, stored, base) -> dict:
    parts, bad = [], False
    for i, axis in enumerate(("X", "Y")):
        if not enabled[i]:
            continue
        want = pre[i] * base[i]
        text = f"{axis} {round(pre[i] * 100, 2):g}%"
        if base[i] > 0:
            text += f" → {num(want)}"
            if abs(want - stored[i]) > 1.5:
                bad = True
                text += f"（存的是 {num(stored[i])}）"
        parts.append(text)
    return row(label, "　".join(parts), status="warn" if bad else "ok",
               note="百分比算出來跟存下來的數值對不上，父層改過尺寸沒重算" if bad
               else "跟存下來的數值一致")


# ---------------------------------------------------------------- 檢查結果

_KINDS = {
    "missingAssets": ("error", "缺少資源",
                      "這些檔案在專案裡找不到，畫面上會缺圖或缺字型。"),
    "atlasMismatch": ("error", "貼圖沒指向圖集",
                      "這裡畫得正常，但 Cocos Studio 開起來會掉圖：圖已打包進圖集，"
                      ".csd 卻沒寫 MarkedSubImage 與 Plist。"),
    "zeroSize": ("error", "元件尺寸是 0",
                 "這裡照貼圖原尺寸畫，但 Cocos Studio 依 Size 排版，0×0 不會顯示。"),
    "textOverflow": ("warn", "文字超出固定框",
                     "文字放不進設計好的框，移植到引擎後同樣會超出。"),
    "staleLayout": ("warn", "版面過期",
                    "百分比與存下來的數值對不上，多半是父層改過尺寸沒重算；"
                    "移植後位置會跟編輯器看到的不同。"),
}


def _refs_of(report: NodeReport) -> set[str]:
    """節點引用的所有資源路徑（含動畫換圖），用來把缺少的資源對回節點。"""
    out: set[str] = set()
    node = report.node
    if node is None:
        return out
    for attrs in node.elements.values():
        for key in ("Path", "Plist"):
            value = (attrs.get(key) or "").replace("\\", "/")
            if value:
                out.add(value)
    doc = report.owner
    if doc is not None and node.action_tag:
        for tl in doc.timelines_by_tag().get(node.action_tag, []):
            for kf in tl.frames:
                if isinstance(kf.value, FileRef):
                    if kf.value.path:
                        out.add(kf.value.path)
                    if kf.value.plist:
                        out.add(kf.value.plist)
    return out


def check_summary(payload: dict, reports: list[NodeReport]) -> dict:
    """把 doctor 的 payload 攤平成 UI 用的問題清單。"""
    groups = []
    for key, (level, title, hint) in _KINDS.items():
        items = []
        for entry in payload.get(key) or []:
            if key == "missingAssets":
                nodes = [r.path for r in reports if entry in _refs_of(r)]
                items.append({"nodes": nodes, "text": entry,
                              "detail": f"{len(nodes)} 個節點用到" if nodes else
                              "用到它的節點在其他影格或巢狀檔案裡"})
            elif key == "atlasMismatch":
                items.append({"nodes": [entry["path"]], "text": entry["path"],
                              "detail": f"{entry['element']} {entry['image']}"
                                        f"  應指向 {entry['expectedPlist']}"})
            elif key == "zeroSize":
                items.append({"nodes": [entry["path"]], "text": entry["path"],
                              "detail": f"{entry['type']} 0×0，貼圖 {entry['image']}"})
            elif key == "textOverflow":
                items.append({"nodes": [entry["path"]], "text": entry["path"],
                              "detail": f"框 {pair(entry['box'], ' × ')}，"
                                        f"實際需要 {pair(entry['needs'], ' × ')}"})
            elif key == "staleLayout":
                detail = "、".join(
                    f"{m['field']} 存 {num(m['stored'])}、百分比算出 {num(m['fromPercent'])}"
                    for m in entry["mismatches"])
                items.append({"nodes": [entry["path"]], "text": entry["path"], "detail": detail})
        if items:
            groups.append({"kind": key, "level": level, "title": title, "hint": hint,
                           "items": items})

    # 每個節點有哪些問題：屬性面板與階層面板的 ⚠ 用
    by_node: dict[str, list[dict]] = {}
    for g in groups:
        for item in g["items"]:
            for path in item["nodes"]:
                by_node.setdefault(path, []).append(
                    {"level": g["level"], "title": g["title"],
                     "detail": item["text"] if g["kind"] == "missingAssets" else item["detail"]})
    return {
        "ok": payload.get("ok", False),
        "errors": sum(len(g["items"]) for g in groups if g["level"] == "error"),
        "warnings": sum(len(g["items"]) for g in groups if g["level"] == "warn"),
        "groups": groups,
        "byNode": by_node,
        "framesProbed": payload.get("framesProbed", []),
    }
