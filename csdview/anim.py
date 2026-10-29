"""時間軸求值：給定一個 frame，算出每個 ActionTag 被動畫覆蓋成什麼值。

Cocos Studio 的動畫是「屬性軌道」制：每條 Timeline 綁一個 ActionTag（節點識別碼）
和一個屬性名，裡面是關鍵影格。渲染時先跑這裡拿到覆蓋值，再套到節點原始屬性上。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import easing
from .model import BlendFunc, Color, Document, Keyframe, Timeline

FPS = 60  # Cocos Studio 時間軸固定 60fps


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_value(a: Any, b: Any, t: float) -> Any:
    if isinstance(a, tuple) and isinstance(b, tuple):
        return tuple(_lerp(x, y, t) for x, y in zip(a, b))
    if isinstance(a, Color) and isinstance(b, Color):
        return Color(
            a=int(round(_lerp(a.a, b.a, t))),
            r=int(round(_lerp(a.r, b.r, t))),
            g=int(round(_lerp(a.g, b.g, t))),
            b=int(round(_lerp(a.b, b.b, t))),
        )
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return _lerp(a, b, t)
    return a  # bool / FileRef / str / dict 這類不做內插


def _bracket(frames: list[Keyframe], frame: float) -> tuple[Keyframe, Keyframe | None, float]:
    """找出 frame 落在哪兩個關鍵影格之間，回傳 (前, 後, 線性進度)。"""
    if frame <= frames[0].index:
        return frames[0], None, 0.0
    if frame >= frames[-1].index:
        return frames[-1], None, 0.0

    lo, hi = 0, len(frames) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if frames[mid].index <= frame:
            lo = mid
        else:
            hi = mid

    a, b = frames[lo], frames[hi]
    span = b.index - a.index
    t = 0.0 if span <= 0 else (frame - a.index) / span
    return a, b, t


def evaluate(tl: Timeline, frame: float) -> Any:
    """求出這條軌道在 frame 當下的值。"""
    if not tl.frames:
        return None
    a, b, t = _bracket(tl.frames, frame)
    if b is None or not a.tween:
        return a.value
    return _lerp_value(a.value, b.value, easing.apply(t, a.easing, a.easing_points))


@dataclass
class TrackState:
    """單一節點在某個 frame 被動畫覆蓋掉的屬性集合。"""
    position: tuple[float, float] | None = None
    scale: tuple[float, float] | None = None
    rotation: tuple[float, float] | None = None
    anchor: tuple[float, float] | None = None
    alpha: float | None = None
    color: Color | None = None
    visible: bool | None = None
    file: Any = None
    blend: BlendFunc | None = None
    inner_action: dict | None = None
    inner_action_frame: int = 0
    event: str = ""


_PROP_FIELDS = {
    "Position": "position",
    "Scale": "scale",
    "RotationSkew": "rotation",
    "AnchorPoint": "anchor",
    "Alpha": "alpha",
    "CColor": "color",
    "VisibleForFrame": "visible",
    "FileData": "file",
    "BlendFunc": "blend",
}


class AnimationState:
    """某個 Document 在某個 frame 的整體動畫狀態。"""

    def __init__(self, doc: Document, frame: float):
        self.doc = doc
        self.frame = frame
        self.tracks: dict[int, TrackState] = {}
        self.events: list[tuple[int, str]] = []

        for tag, timelines in doc.timelines_by_tag().items():
            state = TrackState()
            for tl in timelines:
                if tl.prop == "FrameEvent":
                    kf, _, _ = _bracket(tl.frames, frame)
                    if kf.index <= frame and kf.value:
                        state.event = kf.value
                        self.events.append((tag, kf.value))
                    continue
                if tl.prop == "ActionValue":
                    kf, _, _ = _bracket(tl.frames, frame)
                    if isinstance(kf.value, dict):
                        state.inner_action = kf.value
                        state.inner_action_frame = kf.index
                    continue
                field = _PROP_FIELDS.get(tl.prop)
                if field is None:
                    continue
                value = evaluate(tl, frame)
                if value is not None:
                    setattr(state, field, value)
            self.tracks[tag] = state

    def track(self, action_tag: int) -> TrackState | None:
        return self.tracks.get(action_tag) if action_tag else None


# ---------------------------------------------------------------- 片段工具

def resolve_range(doc: Document, animation: str | None) -> tuple[int, int]:
    """把動畫名稱換成 (起始影格, 結束影格)。"""
    if animation:
        clip = doc.clip(animation)
        if clip is None:
            names = ", ".join(c.name for c in doc.clips) or "(無)"
            raise ValueError(f"找不到動畫 '{animation}'，這個檔案有：{names}")
        return clip.start, clip.end
    if doc.active_animation:
        clip = doc.clip(doc.active_animation)
        if clip:
            return clip.start, clip.end
    return 0, doc.duration
