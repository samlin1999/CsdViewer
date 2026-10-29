"""Cocos Studio .csd 的資料模型。

這一層只描述「檔案裡寫了什麼」，不做任何求值或渲染。
求值在 anim.py，渲染在 render.py。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------- 基本型別

@dataclass
class FileRef:
    """<FileData> / <NormalFileData> / <TextureFile> 這類資源參照。

    type 可能是 Normal / MarkedSubImage / PlistSubImage / Default。
    Normal 與 MarkedSubImage 在編輯器專案裡都是鬆散檔案，直接照 path 讀就好；
    PlistSubImage 必須從 plist 圖集裡拆出 path 這張子圖。
    """
    type: str = "Normal"
    path: str = ""
    plist: str = ""

    @property
    def is_atlas(self) -> bool:
        return self.type == "PlistSubImage" and bool(self.plist)

    def __bool__(self) -> bool:
        return bool(self.path)


@dataclass
class Color:
    a: int = 255
    r: int = 255
    g: int = 255
    b: int = 255

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.r, self.g, self.b, self.a)


@dataclass
class BlendFunc:
    src: int = 770   # GL_SRC_ALPHA
    dst: int = 771   # GL_ONE_MINUS_SRC_ALPHA

    @property
    def is_additive(self) -> bool:
        # dst = GL_ONE 就是加法混色（發光效果最常見的寫法）
        return self.dst == 1


# ---------------------------------------------------------------- 動畫

@dataclass
class Keyframe:
    index: int = 0
    tween: bool = True
    easing: int = 0                       # cocos tweenfunc 列舉
    easing_points: list[tuple[float, float]] | None = None   # 自訂貝茲曲線
    value: Any = None                     # 依 timeline 的 property 而定
    kind: str = ""                        # 原始 XML tag，除錯用


@dataclass
class Timeline:
    action_tag: int = 0
    prop: str = ""                        # Position / Scale / Alpha / ...
    frames: list[Keyframe] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.frames[0].index if self.frames else 0

    @property
    def end(self) -> int:
        return self.frames[-1].index if self.frames else 0


@dataclass
class AnimationClip:
    """<AnimationList> 裡的一段具名動畫。"""
    name: str = ""
    start: int = 0
    end: int = 0

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)


# ---------------------------------------------------------------- 節點

@dataclass
class Node:
    name: str = ""
    ctype: str = "SingleNodeObjectData"
    action_tag: int = 0
    tag: int = 0

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_x: float = 0.0               # RotationSkewX
    rotation_y: float = 0.0               # RotationSkewY

    color: Color = field(default_factory=Color)
    alpha: int = 255
    visible: bool = True
    flip_x: bool = False
    flip_y: bool = False
    blend: BlendFunc | None = None

    # 圖像類
    file: FileRef = field(default_factory=FileRef)          # Sprite / ImageView / Particle / ProjectNode
    normal_file: FileRef = field(default_factory=FileRef)   # Button 常態圖
    scale9: bool = False
    cap_left: float = 0.0
    cap_right: float = 0.0
    cap_top: float = 0.0
    cap_bottom: float = 0.0

    # 文字類
    text: str = ""
    font_size: int = 20
    font_name: str = ""
    font_file: FileRef = field(default_factory=FileRef)     # ttf 或 .fnt
    text_color: Color | None = None
    h_align: str = ""
    v_align: str = ""
    custom_size: bool = False     # IsCustomSize：True 才是固定框，否則跟著文字自動長
    outline_size: int = 0
    outline_color: Color | None = None
    shadow_offset: tuple[float, float] = (0.0, 0.0)
    shadow_color: Color | None = None

    # 容器類
    clip: bool = False
    back_color_enabled: bool = False
    back_color: Color | None = None
    back_color_alpha: int = 255
    # ComboBoxIndex=2 時底色是漸層，用 FirstColor→EndColor，SingleColor 則是無關殘值
    back_gradient: tuple[Color, Color] | None = None
    color_angle: float = 90.0

    # Cocos Studio 的百分比版面：存下來的絕對值理應等於百分比×父層尺寸，
    # 兩者對不上就代表父層改過而這個節點沒重算，是「舊資源」的指紋
    pre_size: tuple[float, float] | None = None
    pre_position: tuple[float, float] | None = None
    percent_size: tuple[bool, bool] = (False, False)
    percent_position: tuple[bool, bool] = (False, False)

    # ProjectNode
    inner_speed: float = 1.0

    # LoadingBar / Slider / CheckBox 等控件的額外資源與參數
    extra_files: dict[str, FileRef] = field(default_factory=dict)
    percent: float = 100.0
    progress_dir: str = "Left_To_Right"

    children: list["Node"] = field(default_factory=list)
    attrs: dict[str, str] = field(default_factory=dict)     # 未解讀的原始屬性

    # ------------------------------------------------------------
    @property
    def size(self) -> tuple[float, float]:
        return (self.width, self.height)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class Document:
    """一個 .csd 檔案。"""
    path: str = ""
    name: str = ""
    version: str = ""
    root_type: str = "Node"               # PropertyGroup Type：Node / Layer / Scene
    root: Node = field(default_factory=Node)
    duration: int = 0
    speed: float = 1.0
    active_animation: str = ""
    timelines: list[Timeline] = field(default_factory=list)
    clips: list[AnimationClip] = field(default_factory=list)

    def timelines_by_tag(self) -> dict[int, list[Timeline]]:
        out: dict[int, list[Timeline]] = {}
        for tl in self.timelines:
            out.setdefault(tl.action_tag, []).append(tl)
        return out

    def clip(self, name: str) -> AnimationClip | None:
        for c in self.clips:
            if c.name == name:
                return c
        return None

    def find(self, name: str) -> Node | None:
        for n in self.root.walk():
            if n.name == name:
                return n
        return None
