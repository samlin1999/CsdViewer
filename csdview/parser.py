"""把 .csd（XML）解析成 model.Document。

支援 Cocos Studio 2.x（根節點 <GameProjectFile>）與 3.x（根節點 <GameFile>），
兩者的 Content/ObjectData/Animation 結構其實一致，只有根 tag 不同。
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET

from .model import (
    AnimationClip, BlendFunc, Color, Document, FileRef, Keyframe, Node, Timeline,
)

# 這些 ctype 有自己的圖檔來源欄位
_TEXT_TYPES = {"TextObjectData", "TextFieldObjectData"}
_BMFONT_TYPES = {"TextBMFontObjectData"}
_BUTTON_TYPES = {"ButtonObjectData", "CheckBoxObjectData"}
_CONTAINER_TYPES = {
    "PanelObjectData", "ScrollViewObjectData", "ListViewObjectData", "PageViewObjectData",
}


# ---------------------------------------------------------------- 屬性小工具

def _f(el: ET.Element | None, key: str, default: float = 0.0) -> float:
    if el is None:
        return default
    v = el.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _i(el: ET.Element | None, key: str, default: int = 0) -> int:
    return int(round(_f(el, key, default)))


def _b(el: ET.Element | None, key: str, default: bool = False) -> bool:
    if el is None:
        return default
    v = el.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1")


def _first(*elements: ET.Element | None) -> ET.Element | None:
    """回傳第一個真正存在的元素。

    不能寫成 `find(A) or find(B)`：ElementTree 的元素在沒有子元素時布林值是 False，
    像 <SingleColor A= R= G= B=/> 這種只有屬性的元素會被判成假，於是錯誤地掉到 B。
    """
    for el in elements:
        if el is not None:
            return el
    return None


def _color(el: ET.Element | None, default: Color | None = None) -> Color | None:
    if el is None:
        return default
    return Color(a=_i(el, "A", 255), r=_i(el, "R", 255), g=_i(el, "G", 255), b=_i(el, "B", 255))


def _file_ref(el: ET.Element | None) -> FileRef:
    if el is None:
        return FileRef()
    return FileRef(
        type=el.get("Type", "Normal"),
        path=(el.get("Path") or "").replace("\\", "/"),
        plist=(el.get("Plist") or "").replace("\\", "/"),
    )


# ---------------------------------------------------------------- 節點

def _parse_node(el: ET.Element) -> Node:
    ctype = el.get("ctype", "SingleNodeObjectData")
    n = Node(
        name=el.get("Name", ""),
        ctype=ctype,
        action_tag=_i(el, "ActionTag", 0),
        tag=_i(el, "Tag", 0),
        alpha=_i(el, "Alpha", 255),
        visible=_b(el, "VisibleForFrame", True) and _b(el, "Visible", True),
        flip_x=_b(el, "FlipX", False),
        flip_y=_b(el, "FlipY", False),
        attrs=dict(el.attrib),
    )

    size = el.find("Size")
    n.width, n.height = _f(size, "X"), _f(size, "Y")

    pos = el.find("Position")
    n.x, n.y = _f(pos, "X"), _f(pos, "Y")

    # AnchorPoint 預設 (0,0)；元素本身可能整個缺席
    anchor = el.find("AnchorPoint")
    n.anchor_x, n.anchor_y = _f(anchor, "ScaleX"), _f(anchor, "ScaleY")

    # Scale 預設 (1,1)，缺屬性代表用預設值
    scale = el.find("Scale")
    n.scale_x, n.scale_y = _f(scale, "ScaleX", 1.0), _f(scale, "ScaleY", 1.0)

    # 沒寫 RotationSkew 時退回 Rotation
    rot = _f(el, "Rotation", 0.0)
    n.rotation_x = _f(el, "RotationSkewX", rot)
    n.rotation_y = _f(el, "RotationSkewY", rot)

    n.color = _color(el.find("CColor"), Color()) or Color()

    pre_size = el.find("PreSize")
    if pre_size is not None:
        n.pre_size = (_f(pre_size, "X"), _f(pre_size, "Y"))
    pre_pos = el.find("PrePosition")
    if pre_pos is not None:
        n.pre_position = (_f(pre_pos, "X"), _f(pre_pos, "Y"))
    n.percent_size = (
        _b(el, "PercentWidthEnable", _b(el, "PercentWidthEnabled")),
        _b(el, "PercentHeightEnable", _b(el, "PercentHeightEnabled")),
    )
    n.percent_position = (_b(el, "PositionPercentXEnabled"),
                          _b(el, "PositionPercentYEnabled"))

    blend = el.find("BlendFunc")
    if blend is not None:
        n.blend = BlendFunc(src=_i(blend, "Src", 770), dst=_i(blend, "Dst", 771))

    # ---- 九宮格
    n.scale9 = _b(el, "Scale9Enable", False)
    if ctype in _BUTTON_TYPES and el.get("Scale9Enable") is None:
        n.scale9 = False
    n.cap_left = _f(el, "LeftEage", _f(el, "Scale9OriginX"))
    n.cap_bottom = _f(el, "BottomEage", _f(el, "Scale9OriginY"))
    n.cap_right = _f(el, "RightEage", 0.0)
    n.cap_top = _f(el, "TopEage", 0.0)

    # ---- 圖像來源
    n.file = _file_ref(el.find("FileData"))
    if ctype in _BUTTON_TYPES:
        source = (el.find("NormalFileData") if ctype == "ButtonObjectData"
                  else el.find("NormalBackFileData"))
        n.normal_file = _file_ref(source if source is not None else el.find("NormalFileData"))
        n.text = el.get("ButtonText", el.get("Text", ""))
        n.font_size = _i(el, "FontSize", 20)
        n.text_color = _color(el.find("TextColor"))
        n.font_file = _file_ref(el.find("FontResource"))
        n.font_name = el.get("FontName", "")
        # CheckBox 的勾選圖疊在底圖上
        for key in ("NodeNormalFileData", "NodeDisableFileData"):
            found = el.find(key)
            if found is not None:
                n.extra_files[key] = _file_ref(found)
    elif ctype == "LoadingBarObjectData":
        n.normal_file = _file_ref(el.find("ImageFileData"))
        n.percent = _f(el, "ProgressInfo", 100.0)
        n.progress_dir = el.get("ProgressType", el.get("Direction", "Left_To_Right"))
    elif ctype == "SliderObjectData":
        n.normal_file = _file_ref(el.find("BackGroundData"))
        n.percent = _f(el, "PercentInfo", 0.0)
        for key in ("ProgressBarData", "BallNormalData"):
            found = el.find(key)
            if found is not None:
                n.extra_files[key] = _file_ref(found)
    elif ctype in _TEXT_TYPES:
        n.text = el.get("LabelText", el.get("PlaceHolderText", ""))
        n.font_size = _i(el, "FontSize", 20)
        n.font_name = el.get("FontName", "")
        n.font_file = _file_ref(el.find("FontResource"))
        n.text_color = n.color
        n.h_align = el.get("HorizontalAlignmentType", "")
        n.v_align = el.get("VerticalAlignmentType", "")
        n.custom_size = _b(el, "IsCustomSize", False)
        n.outline_size = _i(el, "OutlineSize", 0) if _b(el, "OutlineEnabled", False) else 0
        n.outline_color = _color(el.find("OutlineColor"))
        if _b(el, "ShadowEnabled", False):
            n.shadow_offset = (_f(el, "ShadowOffsetX"), _f(el, "ShadowOffsetY"))
            n.shadow_color = _color(el.find("ShadowColor"))
    elif ctype in _BMFONT_TYPES:
        n.text = el.get("LabelText", "")
        n.font_file = _file_ref(el.find("LabelBMFontFile_CNB"))
        n.text_color = n.color
    elif ctype == "ProjectNodeObjectData":
        n.inner_speed = _f(el, "InnerActionSpeed", 1.0)

    if ctype in _CONTAINER_TYPES:
        n.clip = _b(el, "ClipAble", False)
        # ComboBoxIndex：0 無底色、1 單色、2 漸層。XML 幾乎都會帶 SingleColor，
        # 不能單憑它存在就畫底，否則未開背景的 Panel 會出現一條白帶。
        combo = _i(el, "ComboBoxIndex", 0)
        n.back_color_enabled = combo > 0
        n.back_color = _color(_first(el.find("SingleColor"), el.find("FirstColor")))
        n.back_color_alpha = _i(el, "BackColorAlpha", 255)
        n.color_angle = _f(el, "ColorAngle", 90.0)
        if combo == 2:
            first = _color(el.find("FirstColor"))
            end = _color(el.find("EndColor"))
            if first is not None and end is not None:
                n.back_gradient = (first, end)
        # Panel 的底圖走 FileData，且只有 BackGroundImageData 存在時才有圖
        if not n.file:
            n.file = _file_ref(el.find("FileData"))

    # ---- 子節點
    children = el.find("Children")
    if children is not None:
        for child in children:
            if child.tag in ("AbstractNodeData", "ObjectData", "NodeObjectData"):
                n.children.append(_parse_node(child))

    return n


# ---------------------------------------------------------------- 動畫

_FRAME_VALUE_PARSERS = {
    "PointFrame":   lambda el: (_f(el, "X"), _f(el, "Y")),
    # ScaleFrame 同時被 Scale 與 RotationSkew 兩種軌道使用，缺屬性時的預設值不同，
    # 所以真正的預設在 _parse_frame 依 timeline 的 property 補上
    "ScaleFrame":   lambda el: (_f(el, "X"), _f(el, "Y")),
    "IntFrame":     lambda el: _f(el, "Value"),
    "BoolFrame":    lambda el: _b(el, "Value", True),
    "ColorFrame":   lambda el: _color(_first(el.find("Color"),
                                             el.find("RenderColor")), Color()),
    "TextureFrame": lambda el: _file_ref(_first(el.find("TextureFile"),
                                                el.find("FileData"))),
    "BlendFuncFrame": lambda el: BlendFunc(src=_i(el, "Src", 770), dst=_i(el, "Dst", 771)),
    "EventFrame":   lambda el: el.get("Value", ""),
    "InnerActionFrame": lambda el: {
        "type": el.get("InnerActionType", "SingleFrame"),
        "animation": el.get("CurrentAniamtionName", el.get("CurrentAnimationName", "")),
        "frame": _i(el, "SingleFrameIndex", 0),
    },
}


def _parse_frame(el: ET.Element, prop: str = "") -> Keyframe | None:
    parser = _FRAME_VALUE_PARSERS.get(el.tag)
    if parser is None:
        return None

    if el.tag == "ScaleFrame":
        # 縮放缺值代表 1（原尺寸），旋轉／skew 缺值代表 0（不轉）
        fallback = 1.0 if prop == "Scale" else 0.0
        value = (_f(el, "X", fallback), _f(el, "Y", fallback))
    else:
        value = parser(el)

    kf = Keyframe(
        index=_i(el, "FrameIndex", 0),
        tween=_b(el, "Tween", True),
        value=value,
        kind=el.tag,
    )

    easing = el.find("EasingData")
    if easing is not None:
        kf.easing = _i(easing, "Type", 0)
        points = easing.find("Points")
        if points is not None:
            pts = [(_f(p, "X"), _f(p, "Y")) for p in points]
            if pts:
                kf.easing_points = pts
    return kf


def _parse_animation(anim_el: ET.Element | None) -> list[Timeline]:
    if anim_el is None:
        return []
    out: list[Timeline] = []
    for tl_el in anim_el.findall("Timeline"):
        tl = Timeline(action_tag=_i(tl_el, "ActionTag", 0), prop=tl_el.get("Property", ""))
        for frame_el in tl_el:
            kf = _parse_frame(frame_el, tl.prop)
            if kf is not None:
                tl.frames.append(kf)
        tl.frames.sort(key=lambda k: k.index)
        if tl.frames:
            out.append(tl)
    return out


# ---------------------------------------------------------------- 入口

def _read_bytes(path: str, attempts: int = 4) -> bytes:
    """把整個檔案讀進記憶體，握住 handle 的時間壓到最短。

    不用 ET.parse(path)：那會在「整段解析期間」一直開著檔案。Windows 上別的程式
    （例如用 MCP 直接改 .csd 的 agent）此時做原子替換 os.replace 會失敗，
    因為 Python 開檔沒帶 FILE_SHARE_DELETE。改成先讀完就關檔，時間窗小很多。

    也可能反過來——正好在對方替換的瞬間讀到——所以短暫重試幾次。
    """
    for i in range(attempts):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))
    raise AssertionError("unreachable")


def parse(path: str) -> Document:
    """讀一個 .csd 檔並回傳 Document。"""
    return parse_bytes(_read_bytes(path), path)


def parse_bytes(data: bytes, path: str = "") -> Document:
    """從已經讀好的位元組解析。path 只用來填 Document.path 與錯誤訊息。

    編輯器驗證寫入前的內容時不必再繞一趟暫存檔。
    """
    loc = path or "<memory>"
    try:
        root_el = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"{loc}: XML 解析失敗，檔案可能損壞或不是 .csd（{exc}）") from exc

    doc = Document(path=os.path.abspath(path) if path else "")

    pg = root_el.find("PropertyGroup")
    if pg is not None:
        doc.name = pg.get("Name", "")
        doc.version = pg.get("Version", "")
        doc.root_type = pg.get("Type", "Node")
    if not doc.name:
        doc.name = os.path.splitext(os.path.basename(path))[0]

    # <Content ctype="GameProjectContent"><Content> ... </Content></Content>
    # 版本間巢狀層數會變，直接找第一個出現的 ObjectData / Animation 最保險
    obj_el = root_el.find(".//ObjectData")
    if obj_el is None:
        raise ValueError(f"{loc}: 找不到 ObjectData，可能不是有效的 .csd")
    doc.root = _parse_node(obj_el)

    anim_el = root_el.find(".//Animation")
    if anim_el is not None:
        doc.duration = _i(anim_el, "Duration", 0)
        doc.speed = _f(anim_el, "Speed", 1.0) or 1.0
        doc.active_animation = anim_el.get("ActivedAnimationName", "")
        doc.timelines = _parse_animation(anim_el)

    anim_list = root_el.find(".//AnimationList")
    if anim_list is not None:
        for info in anim_list.findall("AnimationInfo"):
            doc.clips.append(AnimationClip(
                name=info.get("Name", ""),
                start=_i(info, "StartIndex", 0),
                end=_i(info, "EndIndex", 0),
            ))

    # 沒有具名片段時，整條時間軸本身就是唯一一段動畫
    if not doc.clips and doc.duration > 0:
        doc.clips.append(AnimationClip(name="(timeline)", start=0, end=doc.duration))

    return doc
