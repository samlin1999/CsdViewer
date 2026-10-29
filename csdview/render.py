"""場景渲染：把 Document 在某個 frame 的狀態畫成圖片。

座標系統依照 Cocos2d-x：原點在左下、Y 軸朝上、旋轉角度為順時針正值。
節點區域變換的公式直接對應 cocos2d-x `Node::getNodeToParentAffineTransform()`，
所以 skew 與非置中錨點的行為都跟編輯器一致。
"""
from __future__ import annotations

import hashlib
import math
import os
import threading
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from . import anim, parser
from .cache import memoize
from .anim import AnimationState
from .assets import AssetLibrary
from .model import Color, Document, FileRef, Node

# 仿射矩陣 (a, b, c, d, tx, ty)：x' = a*x + c*y + tx，y' = b*x + d*y + ty
Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def concat(m1: Matrix, m2: Matrix) -> Matrix:
    """先套用 m2 再套用 m1。"""
    a1, b1, c1, d1, tx1, ty1 = m1
    a2, b2, c2, d2, tx2, ty2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * tx2 + c1 * ty2 + tx1,
        b1 * tx2 + d1 * ty2 + ty1,
    )


def apply_point(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, tx, ty = m
    return (a * x + c * y + tx, b * x + d * y + ty)


def invert(m: Matrix) -> Matrix | None:
    a, b, c, d, tx, ty = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * tx + ic * ty), -(ib * tx + id_ * ty))


def node_matrix(x: float, y: float, scale_x: float, scale_y: float,
                rot_x: float, rot_y: float, anchor_x: float, anchor_y: float,
                width: float, height: float) -> Matrix:
    """節點區域變換，公式同 cocos2d-x Node::getNodeToParentAffineTransform()。"""
    rx = -math.radians(rot_x)
    ry = -math.radians(rot_y)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)

    ax = anchor_x * width * scale_x
    ay = anchor_y * height * scale_y
    tx = x + cy * -ax + -sx * -ay
    ty = y + sy * -ax + cx * -ay
    return (cy * scale_x, sy * scale_x, -sx * scale_y, cx * scale_y, tx, ty)


# ---------------------------------------------------------------- 畫布

class Canvas:
    """預乘 alpha 的 float32 畫布，加法混色才不會出錯。"""

    def __init__(self, width: int, height: int):
        self.width = max(1, width)
        self.height = max(1, height)
        self.buf = np.zeros((self.height, self.width, 4), dtype=np.float32)

    def composite(self, patch: np.ndarray, x: int, y: int, additive: bool = False) -> None:
        ph, pw = patch.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + pw), min(self.height, y + ph)
        if x0 >= x1 or y0 >= y1:
            return

        src = patch[y0 - y:y1 - y, x0 - x:x1 - x]
        dst = self.buf[y0:y1, x0:x1]
        if additive:
            dst += src
        else:
            dst *= (1.0 - src[..., 3:4])
            dst += src

    def to_image(self) -> Image.Image:
        """反預乘並轉成 8-bit 影像。

        大畫布時這一步最吃記憶體：一次做完會連續產生好幾份整張畫布大小的
        float32 暫存陣列。改成逐帶處理，暫存量就只跟單一橫帶成正比。
        """
        out = np.empty((self.height, self.width, 4), dtype=np.uint8)
        rows = max(1, 2_000_000 // max(1, self.width))
        for y0 in range(0, self.height, rows):
            y1 = min(self.height, y0 + rows)
            band = np.clip(self.buf[y0:y1], 0.0, 1.0)
            alpha = band[..., 3:4]
            rgb = band[..., :3]
            np.divide(rgb, np.where(alpha > 1e-6, alpha, 1.0), out=rgb)
            np.clip(band, 0.0, 1.0, out=band)
            band *= 255.0
            band += 0.5
            out[y0:y1] = band.astype(np.uint8)
        return Image.fromarray(out, "RGBA")


def _to_premultiplied(img: Image.Image) -> np.ndarray:
    """轉成預乘 alpha 的 float32 陣列。

    全程原地運算：畫大圖時每個中間陣列都是幾百 MB，
    寫成 `np.asarray(...) / 255.0` 會白白多配置一整份。
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    arr = np.asarray(img, dtype=np.float32)
    arr *= 1.0 / 255.0
    arr[..., :3] *= arr[..., 3:4]
    return arr


# ---------------------------------------------------------------- 節點內容

@dataclass
class Content:
    """節點自身要畫的東西（不含子節點）。

    width/height 是節點的邏輯框（錨點與回報座標都以它為準）；
    圖片實際覆蓋的範圍另外用 offset/image_* 描述，因為文字很可能溢出邏輯框
    ——Cocos Studio 存的是它自己量到的字寬，換成別的字型渲染時會對不上。
    """
    image: Image.Image | None = None
    width: float = 0.0            # 內容在節點座標系中的寬
    height: float = 0.0
    color_baked: bool = False     # True 表示顏色已畫進圖裡，不再套自身 tint
    source: str = ""              # 資源路徑，供報告使用
    offset_x: float = 0.0         # 圖片左下角相對於邏輯框原點的位移
    offset_y: float = 0.0
    image_w: float = 0.0          # 圖片覆蓋的節點座標寬（0 表示同 width）
    image_h: float = 0.0

    def draw_box(self) -> tuple[float, float, float, float]:
        return (self.offset_x, self.offset_y,
                self.image_w or self.width, self.image_h or self.height)


# ---------------------------------------------------------------- 報告

@dataclass
class NodeReport:
    name: str
    path: str
    ctype: str
    action_tag: int
    depth: int
    visible: bool
    alpha: float
    size: tuple[float, float]
    position: tuple[float, float]
    rect: tuple[float, float, float, float]      # 畫布像素座標 x, y, w, h
    quad: list[tuple[float, float]]
    source: str = ""
    text: str = ""
    document: str = ""
    # 內容畫出來的尺寸超過節點框時記在這裡（目前只有固定框文字會發生）。
    # cocos 的 Label 預設 Overflow::NONE，超出高度照樣畫，所以這不是渲染錯誤，
    # 但通常代表文案太長塞不進設計好的框——移植時會在引擎端同樣爆版。
    overflow: tuple[float, float] | None = None
    children: list["NodeReport"] = field(default_factory=list)

    def to_dict(self, with_children: bool = True,
                max_depth: "int | None" = None) -> dict:
        """轉成 JSON 用的 dict。

        max_depth 是「最深輸出到第幾層」，跟 CLI `tree --depth` 的算法一致
        （根節點算第 0 層，所以 max_depth=2 會輸出 0、1、2 三層）。
        被裁掉的節點不是無聲消失：改成留一個 childCount，呼叫端才分得出
        「這個節點沒有小孩」和「有小孩但這次沒送」——沒有這個數字的話，
        拿 depth 限制過的樹去判斷結構會得到錯的結論。
        """
        data = {
            "name": self.name,
            "path": self.path,
            "type": self.ctype.replace("ObjectData", ""),
            "actionTag": self.action_tag,
            "depth": self.depth,
            "visible": self.visible,
            "alpha": round(self.alpha, 3),
            "size": [round(v, 2) for v in self.size],
            "position": [round(v, 2) for v in self.position],
            "screenRect": [round(v, 1) for v in self.rect],
        }
        if self.source:
            data["source"] = self.source
        if self.text:
            data["text"] = self.text
        if self.document:
            data["document"] = self.document
        if self.overflow:
            data["overflowsBox"] = [round(v, 1) for v in self.overflow]
        if with_children and self.children:
            if max_depth is None or self.depth < max_depth:
                data["children"] = [c.to_dict(max_depth=max_depth)
                                    for c in self.children]
            else:
                data["childCount"] = len(self.children)
        return data


@dataclass
class RenderResult:
    image: Image.Image
    frame: float
    animation: str
    root: NodeReport | None
    nodes: list[NodeReport]
    origin: tuple[float, float]
    scale: float
    missing: list[str]
    events: list[str]
    clamped: bool = False        # True 表示畫布過大，已自動降尺度
    design_rect: tuple[float, float, float, float] | None = None   # 設計區在畫布上的位置
    canvas_size: tuple[int, int] | None = None  # 邏輯畫布尺寸；layout_only 時 image 是 1×1


# ---------------------------------------------------------------- 選項

@dataclass
class RenderOptions:
    scale: float = 1.0
    background: str | None = None        # None = 透明；也可給 "#1e1e1e" 或 "checker"
    annotate: bool = False
    annotate_filter: str = ""
    max_depth: int = 16
    padding: int = 0
    # sync＝跟著父層時間跑、first＝固定在第一格、none＝完全不展開巢狀 .csd
    inner_mode: str = "sync"
    draw_particles: bool = True
    full_bounds: bool = False            # 連畫布外的內容一起畫出來
    draw_guide: bool = True              # 畫布被撐大時標出原設計區；做像素比對時必須關掉
    max_canvas: int = 8192               # 單邊像素上限
    max_pixels: int = 16_000_000         # 總像素上限；畫布是 float32 RGBA，很吃記憶體
    layout_only: bool = False            # 只算 screenRect／樹，不畫像素（給 /api/tree）


_PARTICLE_COLOR = (255, 0, 255)


class Renderer:
    def __init__(self, assets: AssetLibrary, options: RenderOptions | None = None):
        self.assets = assets
        self.options = options or RenderOptions()
        # 這些快取會被多執行緒同時取用（批次指令的執行緒池、網頁伺服器的請求執行緒），
        # 一律走 cache.memoize，不要自己寫「查了沒有就算一份存進去」——
        # 那正是先前 _scale9 用 id() 當鍵而畫錯圖的原因。
        self._docs: dict[str, Document | None] = {}
        self._scale9_cache: dict[tuple, Image.Image] = {}
        self._text_cache: dict[tuple, Image.Image] = {}
        self._checker_cache: dict[tuple[int, int], Image.Image] = {}
        self._doc_lock = threading.Lock()
        self._scale9_lock = threading.Lock()
        self._text_lock = threading.Lock()
        self._checker_lock = threading.Lock()

    # ------------------------------------------------------------ 對外入口
    def render(self, doc: Document, frame: float = 0.0,
               animation: str | None = None,
               visibility: dict[str, bool] | None = None) -> RenderResult:
        """visibility 以節點路徑覆寫可視狀態：True 強制顯示、False 強制隱藏。

        強制顯示會略過父層的隱藏狀態，否則想單獨檢視隱藏面板時會看不到東西。
        """
        state = AnimationState(doc, frame)
        vis = visibility or {}

        scale = self.options.scale
        design_w, design_h = doc.root.width, doc.root.height
        has_design = design_w > 0 and design_h > 0

        # 只有需要知道內容範圍時才先量一遍：設計尺寸已知又不畫畫布外內容的話，
        # 那一遍等於把整棵樹白走一次（實測佔單格渲染時間的三成以上）。
        bounds: list[float] = [math.inf, math.inf, -math.inf, -math.inf]
        if self.options.full_bounds or not has_design:
            self._walk(doc, doc.root, state, frame, IDENTITY, 1.0, Color(),
                       True, 0, "", None, None, bounds, set(), vis)

        if has_design and not self.options.full_bounds:
            origin = (0.0, 0.0)
            world_w, world_h = design_w, design_h
        elif bounds[0] < bounds[2]:
            # 內容常被作者擺到設計區外（先擺著、之後在程式裡定位），
            # full_bounds 就是把畫布撐大到連這些都看得到
            pad = self.options.padding
            x0, y0 = bounds[0] - pad, bounds[1] - pad
            x1, y1 = bounds[2] + pad, bounds[3] + pad
            if has_design:
                x0, y0 = min(x0, 0.0), min(y0, 0.0)
                x1, y1 = max(x1, design_w), max(y1, design_h)
            origin = (x0, y0)
            world_w, world_h = x1 - x0, y1 - y0
        elif has_design:
            origin = (0.0, 0.0)
            world_w, world_h = design_w, design_h
        else:
            origin, world_w, world_h = (0.0, 0.0), 1.0, 1.0

        # 兩道上限：單邊像素數與總像素數。畫布是 float32 RGBA，一張 30M 像素的圖
        # 光緩衝區就要 470MB，加上繪製過程的中間陣列會再翻好幾倍，很容易吃光記憶體。
        requested_scale = scale
        canvas_w = max(1, int(round(world_w * scale)))
        canvas_h = max(1, int(round(world_h * scale)))

        limit = self.options.max_canvas
        if limit > 0 and (canvas_w > limit or canvas_h > limit):
            scale *= min(limit / canvas_w, limit / canvas_h)

        cap = self.options.max_pixels
        if cap > 0:
            pixels = (world_w * scale) * (world_h * scale)
            if pixels > cap:
                scale *= math.sqrt(cap / pixels)

        canvas_w = max(1, int(round(world_w * scale)))
        canvas_h = max(1, int(round(world_h * scale)))
        clamped = scale < requested_scale - 1e-9

        # 世界座標（Y 朝上）→ 畫布像素（Y 朝下）
        view: Matrix = (scale, 0.0, 0.0, -scale,
                        -origin[0] * scale, (origin[1] + world_h) * scale)

        canvas = None if self.options.layout_only else Canvas(canvas_w, canvas_h)
        reports: list[NodeReport] = []
        root_report = self._walk(doc, doc.root, state, frame, view, 1.0, Color(),
                                 True, 0, "", canvas, reports, None, set(), vis)

        # 設計區在畫布上的位置，讓 agent 能把圖裁回可比對的範圍
        design_rect = None
        if has_design:
            left, top = apply_point(view, 0.0, design_h)
            right, bottom = apply_point(view, design_w, 0.0)
            design_rect = (left, top, right - left, bottom - top)

        if self.options.layout_only:
            # 樹狀 API 只要 screenRect，不必配置 float32 畫布、也不必合成 PNG。
            # image 留 1×1 佔位，真正尺寸走 canvas_size。
            image = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        else:
            image = self._compose_background(canvas, canvas_w, canvas_h)
            if (self.options.draw_guide and design_rect is not None
                    and (canvas_w, canvas_h) != (int(round(design_w * scale)),
                                                 int(round(design_h * scale)))):
                self._draw_design_guide(image, design_rect)
            if self.options.annotate:
                self._annotate(image, reports)

        return RenderResult(
            image=image,
            frame=frame,
            animation=animation or "",
            root=root_report,
            nodes=reports,
            origin=origin,
            scale=scale,
            missing=sorted(self.assets.missing),
            events=[value for _tag, value in state.events if value],
            clamped=clamped,
            design_rect=design_rect,
            canvas_size=(canvas_w, canvas_h),
        )

    # ------------------------------------------------------------ 背景
    def _compose_background(self, canvas: Canvas, w: int, h: int) -> Image.Image:
        image = canvas.to_image()
        bg = self.options.background
        if not bg or bg == "none":
            return image
        if bg == "checker":
            base = self._checker(w, h).copy()
        else:
            base = Image.new("RGBA", (w, h), _parse_color(bg))
        base.alpha_composite(image)
        return base

    def _draw_design_guide(self, image: Image.Image,
                           rect: tuple[float, float, float, float]) -> None:
        """畫布被撐大時，用虛線標出原本的設計區範圍。"""
        x, y, w, h = rect
        draw = ImageDraw.Draw(image, "RGBA")
        x0, y0, x1, y1 = round(x), round(y), round(x + w) - 1, round(y + h) - 1
        dash, gap, colour = 8, 6, (0, 220, 255, 150)
        for px in range(x0, x1, dash + gap):
            draw.line([px, y0, min(px + dash, x1), y0], fill=colour)
            draw.line([px, y1, min(px + dash, x1), y1], fill=colour)
        for py in range(y0, y1, dash + gap):
            draw.line([x0, py, x0, min(py + dash, y1)], fill=colour)
            draw.line([x1, py, x1, min(py + dash, y1)], fill=colour)

    def _checker(self, width: int, height: int) -> Image.Image:
        """格子背景只跟尺寸有關，做一次就好。

        原本每格影格都用 Python 迴圈重畫一次（960x640 要 2400 次 draw），
        實測佔單格渲染時間近兩成。
        """
        def build() -> Image.Image:
            tile = 16
            base = Image.new("RGBA", (width, height), (70, 70, 74, 255))
            cell = Image.new("RGBA", (tile, tile), (90, 90, 96, 255))
            for y in range(0, height, tile):
                for x in range(0, width, tile):
                    if ((x // tile) + (y // tile)) % 2 == 0:
                        base.paste(cell, (x, y))
            return base

        return memoize(self._checker_cache, (width, height),
                       self._checker_lock, build, limit=8)

    # ------------------------------------------------------------ 巡訪
    def _walk(self, doc: Document, node: Node, state: AnimationState, frame: float,
              parent_matrix: Matrix, parent_alpha: float, parent_color: Color,
              parent_visible: bool, depth: int, path_prefix: str,
              canvas: Canvas | None, reports: list[NodeReport] | None,
              bounds: list[float] | None, doc_stack: set[str],
              visibility: dict[str, bool]) -> NodeReport | None:
        """遞迴走訪節點樹；canvas 為 None 時只量尺寸／建報告，不畫。"""
        if depth > self.options.max_depth:
            return None

        track = state.track(node.action_tag)

        # ---- 動畫覆蓋
        x, y = node.x, node.y
        scale_x, scale_y = node.scale_x, node.scale_y
        rot_x, rot_y = node.rotation_x, node.rotation_y
        anchor_x, anchor_y = node.anchor_x, node.anchor_y
        alpha = float(node.alpha)
        color = node.color
        visible = node.visible
        file_ref = node.file
        blend = node.blend

        if track is not None:
            if track.position is not None:
                x, y = track.position
            if track.scale is not None:
                scale_x, scale_y = track.scale
            if track.rotation is not None:
                rot_x, rot_y = track.rotation
            if track.anchor is not None:
                anchor_x, anchor_y = track.anchor
            if track.alpha is not None:
                alpha = track.alpha
            if track.color is not None:
                color = track.color
            if track.visible is not None:
                visible = track.visible
            if track.file is not None and isinstance(track.file, FileRef) and track.file.path:
                file_ref = track.file
            if track.blend is not None:
                blend = track.blend

        node_path = f"{path_prefix}/{node.name}" if path_prefix else node.name
        override = visibility.get(node_path)
        if override is None:
            effective_visible = parent_visible and visible
        else:
            # 強制顯示要能穿透父層的隱藏，才看得到被藏起來的面板
            effective_visible = override
        node_alpha = parent_alpha * (alpha / 255.0) * (color.a / 255.0)
        node_color = Color(
            a=255,
            r=int(color.r * parent_color.r / 255),
            g=int(color.g * parent_color.g / 255),
            b=int(color.b * parent_color.b / 255),
        )

        content = self._content_for(node, file_ref, color)
        content_w = content.width or node.width
        content_h = content.height or node.height

        local = node_matrix(x, y, scale_x, scale_y, rot_x, rot_y,
                            anchor_x, anchor_y, content_w, content_h)
        world = concat(parent_matrix, local)

        # ---- 畫自己
        quad = [apply_point(world, 0, 0), apply_point(world, content_w, 0),
                apply_point(world, content_w, content_h), apply_point(world, 0, content_h)]
        rect = _quad_rect(quad)

        if bounds is not None and content.image is not None and effective_visible:
            ox, oy, dw, dh = content.draw_box()
            for px, py in (apply_point(world, ox, oy), apply_point(world, ox + dw, oy),
                           apply_point(world, ox + dw, oy + dh), apply_point(world, ox, oy + dh)):
                bounds[0] = min(bounds[0], px)
                bounds[1] = min(bounds[1], py)
                bounds[2] = max(bounds[2], px)
                bounds[3] = max(bounds[3], py)

        report = NodeReport(
            name=node.name, path=node_path, ctype=node.ctype, action_tag=node.action_tag,
            depth=depth, visible=effective_visible, alpha=node_alpha,
            size=(content_w, content_h), position=(x, y), rect=rect, quad=quad,
            source=content.source, text=node.text,
        )
        if node.ctype in ("TextObjectData", "TextFieldObjectData") and node.custom_size:
            _ox, _oy, draw_w, draw_h = content.draw_box()
            if draw_w > content_w + 2 or draw_h > content_h + 2:
                report.overflow = (draw_w, draw_h)
        if reports is not None:
            reports.append(report)

        if canvas is not None and content.image is not None and effective_visible and node_alpha > 0.002:
            tint = None if content.color_baked else node_color
            off_x, off_y, draw_w, draw_h = content.draw_box()
            draw_matrix = concat(world, (1.0, 0.0, 0.0, 1.0, off_x, off_y))
            self._draw(canvas, content.image, draw_matrix, draw_w, draw_h,
                       node_alpha, tint if tint else parent_color,
                       additive=bool(blend and blend.is_additive),
                       flip_x=node.flip_x, flip_y=node.flip_y)

        # ---- 子節點（clip 時先畫到暫存畫布再裁切）
        target = canvas
        clip_active = bool(node.clip) and canvas is not None and content_w > 0 and content_h > 0

        # 量尺寸時也要考慮裁切：ScrollView／PageView 的內容常常遠超出自己的框，
        # 若照原樣算進邊界，畫布會為了容納「畫完就被裁掉」的東西而暴增
        # （實測有 PageView 讓 1400 寬的畫面變成 4478）。
        clip_bounds = None
        child_bounds = bounds
        if bounds is not None and node.clip and content_w > 0 and content_h > 0:
            clip_bounds = [math.inf, math.inf, -math.inf, -math.inf]
            child_bounds = clip_bounds

        if clip_active:
            target = Canvas(canvas.width, canvas.height)

        for child in node.children:
            child_report = self._walk(doc, child, state, frame, world, node_alpha, node_color,
                                      effective_visible, depth + 1, node_path,
                                      target, reports, child_bounds, doc_stack, visibility)
            if child_report is not None:
                report.children.append(child_report)

        # ---- ProjectNode：把引用的 .csd 當成子樹接上來
        if (node.ctype == "ProjectNodeObjectData" and self.options.inner_mode != "none"
                and file_ref and file_ref.path.endswith(".csd")):
            inner = self._load_document(file_ref.path)
            if inner is not None and inner.path not in doc_stack:
                inner_frame = self._inner_frame(inner, track, frame, node.inner_speed)
                inner_state = AnimationState(inner, inner_frame)
                inner_report = self._walk(
                    inner, inner.root, inner_state, inner_frame, world, node_alpha, node_color,
                    effective_visible, depth + 1, node_path, target, reports, child_bounds,
                    doc_stack | {inner.path}, visibility)
                if inner_report is not None:
                    inner_report.document = os.path.basename(inner.path)
                    report.children.append(inner_report)
                report.document = os.path.basename(inner.path)

        if clip_bounds is not None and bounds is not None and clip_bounds[0] < clip_bounds[2]:
            # 子樹的範圍要先跟裁切框取交集，再併回父層
            box = _quad_rect(quad)
            x0 = max(clip_bounds[0], box[0])
            y0 = max(clip_bounds[1], box[1])
            x1 = min(clip_bounds[2], box[0] + box[2])
            y1 = min(clip_bounds[3], box[1] + box[3])
            if x0 < x1 and y0 < y1:
                bounds[0] = min(bounds[0], x0)
                bounds[1] = min(bounds[1], y0)
                bounds[2] = max(bounds[2], x1)
                bounds[3] = max(bounds[3], y1)

        if clip_active and target is not None and canvas is not None:
            self._apply_clip(target, quad)
            canvas.composite(target.buf, 0, 0)

        return report

    # ------------------------------------------------------------ 實際繪製
    def _draw(self, canvas: Canvas, image: Image.Image, world: Matrix,
              content_w: float, content_h: float, alpha: float, tint: Color,
              additive: bool, flip_x: bool, flip_y: bool) -> None:
        if content_w <= 0 or content_h <= 0:
            return
        if flip_x:
            image = image.transpose(Image.FLIP_LEFT_RIGHT)
        if flip_y:
            image = image.transpose(Image.FLIP_TOP_BOTTOM)

        pw, ph = image.size
        if pw <= 0 or ph <= 0:
            return

        # 圖片像素座標（Y 朝下）→ 節點座標（Y 朝上）
        pixel_to_node: Matrix = (content_w / pw, 0.0, 0.0, -content_h / ph, 0.0, content_h)
        forward = concat(world, pixel_to_node)
        inverse = invert(forward)
        if inverse is None:
            return

        corners = [apply_point(forward, 0, 0), apply_point(forward, pw, 0),
                   apply_point(forward, pw, ph), apply_point(forward, 0, ph)]
        x0 = max(0, int(math.floor(min(p[0] for p in corners))))
        y0 = max(0, int(math.floor(min(p[1] for p in corners))))
        x1 = min(canvas.width, int(math.ceil(max(p[0] for p in corners))) + 1)
        y1 = min(canvas.height, int(math.ceil(max(p[1] for p in corners))) + 1)
        if x0 >= x1 or y0 >= y1:
            return

        ia, ib, ic, id_, itx, ity = inverse
        data = (ia, ic, ia * x0 + ic * y0 + itx,
                ib, id_, ib * x0 + id_ * y0 + ity)

        # 縮小很多時先降採樣，避免鋸齒
        span = max(abs(corners[1][0] - corners[0][0]), abs(corners[1][1] - corners[0][1]))
        if span > 0 and pw / max(span, 1e-6) > 2:
            factor = max(1, int(pw / max(span, 1.0)))
            if factor > 1:
                image = image.reduce(min(factor, 8))
                pw, ph = image.size
                pixel_to_node = (content_w / pw, 0.0, 0.0, -content_h / ph, 0.0, content_h)
                inverse = invert(concat(world, pixel_to_node))
                if inverse is None:
                    return
                ia, ib, ic, id_, itx, ity = inverse
                data = (ia, ic, ia * x0 + ic * y0 + itx,
                        ib, id_, ib * x0 + id_ * y0 + ity)

        patch = image.transform((x1 - x0, y1 - y0), Image.AFFINE, data,
                                resample=Image.BILINEAR)
        arr = _to_premultiplied(patch)
        if alpha < 1.0:
            arr *= alpha
        if tint and (tint.r, tint.g, tint.b) != (255, 255, 255):
            arr[..., 0] *= tint.r / 255.0
            arr[..., 1] *= tint.g / 255.0
            arr[..., 2] *= tint.b / 255.0
        canvas.composite(arr, x0, y0, additive=additive)

    def _apply_clip(self, layer: Canvas, quad: list[tuple[float, float]]) -> None:
        mask = Image.new("L", (layer.width, layer.height), 0)
        ImageDraw.Draw(mask).polygon([(round(px), round(py)) for px, py in quad], fill=255)
        layer.buf *= (np.asarray(mask, dtype=np.float32) / 255.0)[..., None]

    # ------------------------------------------------------------ 內容建構
    def _content_for(self, node: Node, file_ref: FileRef, color: Color) -> Content:
        ctype = node.ctype

        if ctype in ("SpriteObjectData", "ImageViewObjectData", "PanelObjectData",
                     "ScrollViewObjectData", "ListViewObjectData", "PageViewObjectData"):
            return self._image_content(node, file_ref)

        if ctype in ("ButtonObjectData", "CheckBoxObjectData"):
            return self._button_content(node, color)

        if ctype == "LoadingBarObjectData":
            return self._loading_bar_content(node)

        if ctype == "SliderObjectData":
            return self._image_content(node, node.normal_file)

        if ctype == "TextObjectData" or ctype == "TextFieldObjectData":
            return self._text_content(node, color)

        if ctype == "TextBMFontObjectData":
            return self._bmfont_content(node)

        if ctype == "ParticleObjectData":
            return self._particle_content(node)

        return Content(width=node.width, height=node.height)

    def _image_content(self, node: Node, file_ref: FileRef) -> Content:
        source = file_ref if file_ref and file_ref.path else node.file
        image = self.assets.image(source) if source and source.path else None

        if image is None:
            if (node.back_color_enabled and node.back_color_alpha > 0
                    and node.width > 0 and node.height > 0):
                size = (max(1, int(node.width)), max(1, int(node.height)))
                if node.back_gradient is not None:
                    plate = _gradient_plate(size, node.back_gradient[0],
                                            node.back_gradient[1], node.color_angle,
                                            node.back_color_alpha)
                    return Content(plate, node.width, node.height, color_baked=False)
                if node.back_color is not None:
                    fill = (node.back_color.r, node.back_color.g, node.back_color.b,
                            node.back_color_alpha)
                    plate = Image.new("RGBA", size, fill)
                    return Content(plate, node.width, node.height, color_baked=False)
            return Content(width=node.width, height=node.height)

        if node.scale9 and node.width > 0 and node.height > 0:
            image = self._scale9(image, node, f"{source.path}|{source.plist}")
            return Content(image, node.width, node.height, source=source.path)

        img_w, img_h = float(image.size[0]), float(image.size[1])

        # Default 佔位圖不是專案資源，執行時不會出現。邏輯框仍保留，方便 spec／座標，
        # 但不要畫上去，否則會蓋住底下真正的版面。
        if source.type == "Default":
            return Content(width=node.width or img_w, height=node.height or img_h,
                           source=source.path)

        # ImageView／Panel／ListView 等控件以節點 Size 為準，貼圖要拉滿；
        # Sprite 則維持貼圖原尺寸（縮放走 ScaleX/Y）。
        if node.ctype != "SpriteObjectData" and node.width > 0 and node.height > 0:
            return Content(image, node.width, node.height, source=source.path)

        width, height = image.size
        return Content(image, float(width), float(height), source=source.path)

    def _button_content(self, node: Node, color: Color) -> Content:
        base = node.normal_file if node.normal_file else node.file
        image = self.assets.image(base) if base and base.path else None
        width = node.width or (image.size[0] if image else 0)
        height = node.height or (image.size[1] if image else 0)

        if image is None and not node.text:
            return Content(width=width, height=height)

        if image is not None and node.scale9 and node.width > 0 and node.height > 0:
            image = self._scale9(image, node,
                                 f"{base.path}|{base.plist}" if base else "")
        # 沒開九宮格也要畫在節點 Size 裡；不要改用貼圖原尺寸，否則按鈕會爆大。

        canvas_size = (max(1, int(round(width))), max(1, int(round(height))))
        plate = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        if image is not None:
            if image.size != canvas_size:
                image = image.resize(canvas_size, Image.BILINEAR)
            # 文字會被烘進同一張圖，所以底圖的染色要在這裡先做掉
            plate.alpha_composite(_tinted(image, color) if node.text else image)

        # 勾選圖（CheckBox）
        for key in ("NodeNormalFileData",):
            ref = node.extra_files.get(key)
            if ref and ref.path:
                mark = self.assets.image(ref)
                if mark is not None:
                    plate.alpha_composite(mark, (
                        max(0, (canvas_size[0] - mark.size[0]) // 2),
                        max(0, (canvas_size[1] - mark.size[1]) // 2)))

        if node.text:
            label = self._render_text(
                node.text, node.font_file, node.font_name, node.font_size,
                node.text_color or Color(), "", 0, None, (0, 0), None)
            if label is not None:
                lx = (canvas_size[0] - label.size[0]) // 2
                ly = (canvas_size[1] - label.size[1]) // 2
                if lx < 0 or ly < 0:
                    # 字比按鈕寬時 alpha_composite 不接受負座標，改用 paste
                    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
                    layer.paste(label, (lx, ly), label)
                    plate.alpha_composite(layer)
                else:
                    plate.alpha_composite(label, (lx, ly))

        # 圖已含彩色文字，只讓父層 tint 生效
        return Content(plate, float(canvas_size[0]), float(canvas_size[1]),
                       color_baked=bool(node.text), source=base.path if base else "")

    def _loading_bar_content(self, node: Node) -> Content:
        image = self.assets.image(node.normal_file) if node.normal_file else None
        if image is None:
            return Content(width=node.width, height=node.height)
        percent = max(0.0, min(100.0, node.percent)) / 100.0
        width = max(1, int(round(image.size[0] * percent)))
        if "Right" in node.progress_dir and "Left" in node.progress_dir.split("_To_")[0]:
            crop = image.crop((0, 0, width, image.size[1]))
            plate = Image.new("RGBA", image.size, (0, 0, 0, 0))
            plate.alpha_composite(crop)
        else:
            crop = image.crop((image.size[0] - width, 0, image.size[0], image.size[1]))
            plate = Image.new("RGBA", image.size, (0, 0, 0, 0))
            plate.alpha_composite(crop, (image.size[0] - width, 0))
        return Content(plate, float(image.size[0]), float(image.size[1]),
                       source=node.normal_file.path)

    def _scale9(self, image: Image.Image, node: Node,
                source_key: str = "") -> Image.Image:
        """九宮格拉伸。

        快取鍵用資源路徑而不是 id(image)：物件被回收後 id 會被重複使用，
        多執行緒批次渲染時就會拿到別張圖的結果（實測 8 執行緒下 12 次有 7 次出錯）。
        沒有路徑時改用內容指紋——絕不能退回 id()。
        """
        target = (max(1, int(round(node.width))), max(1, int(round(node.height))))
        identity = source_key or hashlib.sha1(image.tobytes()).hexdigest()
        key = (identity, image.size, target,
               node.cap_left, node.cap_right, node.cap_top, node.cap_bottom)
        return memoize(self._scale9_cache, key, self._scale9_lock,
                       lambda: self._build_scale9(image, node, target), limit=128)

    def _build_scale9(self, image: Image.Image, node: Node,
                      target: tuple[int, int]) -> Image.Image:
        src_w, src_h = image.size
        left = int(max(0, min(node.cap_left, src_w - 1)))
        right = int(max(0, min(node.cap_right, src_w - 1 - left)))
        top = int(max(0, min(node.cap_top, src_h - 1)))
        bottom = int(max(0, min(node.cap_bottom, src_h - 1 - top)))
        if left + right == 0 and top + bottom == 0:
            return image.resize(target, Image.BILINEAR)

        dst_w, dst_h = target
        mid_dst_w = max(0, dst_w - left - right)
        mid_dst_h = max(0, dst_h - top - bottom)

        xs_src = [0, left, src_w - right, src_w]
        ys_src = [0, top, src_h - bottom, src_h]
        xs_dst = [0, left, left + mid_dst_w, dst_w]
        ys_dst = [0, top, top + mid_dst_h, dst_h]

        result = Image.new("RGBA", target, (0, 0, 0, 0))
        for row in range(3):
            for col in range(3):
                sx0, sx1 = xs_src[col], xs_src[col + 1]
                sy0, sy1 = ys_src[row], ys_src[row + 1]
                dx0, dx1 = xs_dst[col], xs_dst[col + 1]
                dy0, dy1 = ys_dst[row], ys_dst[row + 1]
                if sx1 <= sx0 or sy1 <= sy0 or dx1 <= dx0 or dy1 <= dy0:
                    continue
                piece = image.crop((sx0, sy0, sx1, sy1))
                if piece.size != (dx1 - dx0, dy1 - dy0):
                    piece = piece.resize((dx1 - dx0, dy1 - dy0), Image.BILINEAR)
                result.paste(piece, (dx0, dy0))
        return result

    # ------------------------------------------------------------ 文字
    def _text_content(self, node: Node, color: Color) -> Content:
        # IsCustomSize 的 Label 在 cocos 會自動斷行；沒有固定框的則不換行
        wrap_at = node.width if (node.custom_size and node.width > 0) else 0.0
        image = self._render_text(
            node.text, node.font_file, node.font_name, node.font_size,
            color, node.h_align, node.outline_size, node.outline_color,
            node.shadow_offset, node.shadow_color, wrap_at)
        if image is None:
            return Content(width=node.width, height=node.height)

        img_w, img_h = float(image.size[0]), float(image.size[1])

        # 沒有 IsCustomSize 的 Text 是「自動尺寸」：Cocos 用它自己量到的字寬當內容
        # 尺寸，再套錨點決定位置，.csd 裡的 Size 只是那次測量的結果。我們換了字型，
        # 量到的尺寸必然不同，所以要用自己量到的尺寸當內容框——錨點才會把文字擺到
        # 同一個中心。沿用設計尺寸並靠邊對齊的話，字寬一有落差就會整體偏移。
        if not node.custom_size or node.width <= 0 or node.height <= 0:
            return Content(image, img_w, img_h, color_baked=True)

        # IsCustomSize="True"：框是設計者固定的，文字依對齊方式擺進框裡，溢出不裁切
        box_w, box_h = node.width, node.height
        if "Right" in node.h_align:
            offset_x = box_w - img_w
        elif "Center" in node.h_align:
            offset_x = (box_w - img_w) / 2
        else:
            offset_x = 0.0

        # 節點座標 Y 朝上，所以「靠上」是大的 Y
        if "Bottom" in node.v_align:
            offset_y = 0.0
        elif "Center" in node.v_align:
            offset_y = (box_h - img_h) / 2
        else:
            offset_y = box_h - img_h

        return Content(image, box_w, box_h, color_baked=True,
                       offset_x=offset_x, offset_y=offset_y,
                       image_w=img_w, image_h=img_h)

    def _render_text(self, text: str, font_ref: FileRef, font_name: str, size: int,
                     color: Color, h_align: str, outline: int, outline_color: Color | None,
                     shadow_offset: tuple[float, float],
                     shadow_color: Color | None,
                     wrap_at: float = 0.0) -> Image.Image | None:
        """把文字畫成圖片。wrap_at > 0 時自動斷行，否則不限制寬度。"""
        if not text:
            return None
        key = (text, font_ref.path if font_ref else "", font_name, size,
               color.as_tuple(), h_align, outline,
               outline_color.as_tuple() if outline_color else None, shadow_offset,
               shadow_color.as_tuple() if shadow_color else None,
               round(wrap_at))
        return memoize(
            self._text_cache, key, self._text_lock,
            lambda: self._rasterize_text(text, font_ref, font_name, size, color,
                                         h_align, outline, outline_color,
                                         shadow_offset, shadow_color, wrap_at),
            limit=128)

    def _rasterize_text(self, text: str, font_ref: FileRef, font_name: str,
                        size: int, color: Color, h_align: str, outline: int,
                        outline_color: Color | None,
                        shadow_offset: tuple[float, float],
                        shadow_color: Color | None,
                        wrap_at: float = 0.0) -> Image.Image | None:
        font = self.assets.truetype(font_ref, font_name, size)
        if font is None:
            return None

        lines = text.split("\n")
        if wrap_at > 0:
            lines = _wrap_lines(lines, font, wrap_at)
        draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        widths = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line or " ", font=font)
            widths.append(bbox[2])
        # 行距用字級本身：拿 1161 個單行文字節點比對過 Cocos Studio 的設計高度，
        # 「設計高度 ÷ FontSize」中位數正好是 1.000。用 ascent+descent 會多出約
        # 三成，多行文字的行距就明顯偏鬆，而且會誤判成塞不進固定框。
        line_advance = max(1, size)
        try:
            ascent, descent = font.getmetrics()
            ink_height = max(1, ascent + descent)
        except (AttributeError, OSError):
            ink_height = max(1, int(round(size * 1.25)))

        # 外框、陰影都會往外長，留出對應的邊界
        margin = outline + int(max(abs(shadow_offset[0]), abs(shadow_offset[1])))
        width = max(1, (max(widths) if widths else 1) + margin * 2)
        # 最後一行的字仍需要完整的墨跡高度，不能只留一個行距
        height = max(1, line_advance * (len(lines) - 1) + ink_height + margin * 2)

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(lines):
            if "Right" in h_align:
                x = width - margin - widths[index]
            elif "Center" in h_align:
                x = (width - widths[index]) / 2
            else:
                x = margin
            y = margin + index * line_advance

            if shadow_color is not None and shadow_offset != (0, 0):
                draw.text((x + shadow_offset[0], y - shadow_offset[1]), line,
                          font=font, fill=shadow_color.as_tuple())
            if outline > 0 and outline_color is not None:
                draw.text((x, y), line, font=font, fill=color.as_tuple(),
                          stroke_width=outline, stroke_fill=outline_color.as_tuple())
            else:
                draw.text((x, y), line, font=font, fill=color.as_tuple())

        return image

    def _bmfont_content(self, node: Node) -> Content:
        font = self.assets.bmfont(node.font_file)
        if font is None or not node.text:
            return Content(width=node.width, height=node.height)

        codes = [ord(ch) for ch in node.text]
        total = 0
        prev = None
        for code in codes:
            glyph = font.glyphs.get(code)
            if glyph is None:
                continue
            if prev is not None:
                total += font.kernings.get((prev, code), 0)
            total += glyph.xadvance
            prev = code
        height = font.line_height or max((g.h for g in font.glyphs.values()), default=1)
        image = Image.new("RGBA", (max(1, total), max(1, height)), (0, 0, 0, 0))

        pen = 0
        prev = None
        for code in codes:
            glyph = font.glyphs.get(code)
            if glyph is None:
                continue
            if prev is not None:
                pen += font.kernings.get((prev, code), 0)
            page = font.pages.get(glyph.page)
            if page is not None and glyph.w > 0 and glyph.h > 0:
                piece = page.crop((glyph.x, glyph.y, glyph.x + glyph.w, glyph.y + glyph.h))
                image.alpha_composite(piece, (pen + glyph.xoffset, glyph.yoffset))
            pen += glyph.xadvance
            prev = code

        return Content(image, float(image.size[0]), float(image.size[1]),
                       source=node.font_file.path)

    def _particle_content(self, node: Node) -> Content:
        if not self.options.draw_particles:
            return Content(width=node.width, height=node.height)
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle([1, 1, size - 2, size - 2], outline=_PARTICLE_COLOR + (200,), width=2)
        draw.line([size // 2, 8, size // 2, size - 8], fill=_PARTICLE_COLOR + (140,))
        draw.line([8, size // 2, size - 8, size // 2], fill=_PARTICLE_COLOR + (140,))
        return Content(image, float(size), float(size), color_baked=True,
                       source=node.file.path if node.file else "")

    # ------------------------------------------------------------ 巢狀 csd
    def _load_document(self, rel_path: str) -> Document | None:
        full = self.assets.resolve(rel_path)
        if full is None:
            self.assets.missing.add(rel_path)
            return None
        def build() -> Document | None:
            try:
                return parser.parse(full)
            except Exception:
                return None

        return memoize(self._docs, full, self._doc_lock, build, limit=64)

    def _inner_frame(self, inner: Document, track, parent_frame: float,
                     speed: float) -> float:
        speed = speed or 1.0
        if track is not None and track.inner_action:
            info = track.inner_action
            name = info.get("animation", "")
            clip = inner.clip(name) if name and not name.startswith("--") else None
            start, end = (clip.start, clip.end) if clip else (0, inner.duration)
            length = max(0, end - start)
            kind = info.get("type", "SingleFrame")
            if kind == "SingleFrame":
                return float(info.get("frame", 0))
            elapsed = (parent_frame - track.inner_action_frame) * speed
            if length <= 0:
                return float(start)
            if kind == "Loop":
                return start + (elapsed % length)
            return start + max(0.0, min(elapsed, length))

        if self.options.inner_mode == "first":
            return 0.0
        if inner.duration > 0:
            return (parent_frame * speed) % inner.duration
        return 0.0

    # ------------------------------------------------------------ 標註
    def _annotate(self, image: Image.Image, reports: list[NodeReport]) -> None:
        draw = ImageDraw.Draw(image, "RGBA")
        font = None
        try:
            from .assets import system_font_path
            path = system_font_path()
            if path:
                from PIL import ImageFont
                font = ImageFont.truetype(path, 12)
        except Exception:
            font = None

        needle = self.options.annotate_filter.lower()
        for report in reports:
            if not report.visible or report.size[0] <= 0 or report.size[1] <= 0:
                continue
            if needle and needle not in report.name.lower() and needle not in report.path.lower():
                continue
            points = [(round(px), round(py)) for px, py in report.quad]
            draw.polygon(points, outline=(0, 220, 255, 190))
            label = report.name or report.ctype.replace("ObjectData", "")
            x, y = report.rect[0], report.rect[1]
            if font is not None:
                bbox = draw.textbbox((0, 0), label, font=font)
                w, h = bbox[2] - bbox[0] + 6, bbox[3] - bbox[1] + 4
                draw.rectangle([x, y - h, x + w, y], fill=(0, 0, 0, 170))
                draw.text((x + 3, y - h + 1), label, font=font, fill=(255, 255, 255, 235))
            else:
                draw.text((x + 2, y + 2), label, fill=(255, 255, 255, 235))


# ---------------------------------------------------------------- 小工具

def _gradient_plate(size: tuple[int, int], first: Color, end: Color,
                    angle: float, alpha: int) -> Image.Image:
    """畫 Panel 的漸層底色（ComboBoxIndex=2）。

    方向以螢幕座標（Y 向下）的單位向量 (cos θ, sin θ) 定義：θ=90 就是由上而下，
    FirstColor 在起點。資料集裡 22 個漸層節點的 ColorAngle 全是 90，
    其他角度屬於標準推論、未經實物驗證。
    """
    width, height = size
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)

    xs = np.arange(width, dtype=np.float32).reshape(1, width)
    ys = np.arange(height, dtype=np.float32).reshape(height, 1)
    proj = xs * dx + ys * dy
    span = proj.max() - proj.min()
    ratio = (proj - proj.min()) / (span if span > 1e-6 else 1.0)
    ratio = np.broadcast_to(ratio, (height, width))[..., None]

    a = np.array([first.r, first.g, first.b], dtype=np.float32)
    b = np.array([end.r, end.g, end.b], dtype=np.float32)
    rgb = a + (b - a) * ratio
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb + 0.5, 0, 255).astype(np.uint8)
    out[..., 3] = max(0, min(255, alpha))
    return Image.fromarray(out, "RGBA")


def _wrap_lines(paragraphs: list[str], font, max_width: float) -> list[str]:
    """把每段文字斷成不超過 max_width 的多行。

    寬度用逐字累加字寬（與 cocos 的排版一致），不是整行量測——
    整行量測是 O(n^2) 次呼叫，長文字會很慢。
    中日文可在任意字元換行，西文則盡量退回最近的空白，避免把單字切開。
    """
    advance: dict[str, float] = {}

    def width_of(ch: str) -> float:
        got = advance.get(ch)
        if got is None:
            got = float(font.getlength(ch))
            advance[ch] = got
        return got

    def is_word_char(ch: str) -> bool:
        return ch.isascii() and (ch.isalnum() or ch in "-_'")

    out: list[str] = []
    for para in paragraphs:
        if not para:
            out.append("")
            continue
        current = ""
        used = 0.0
        space_at = -1                    # 最後一個可換行的空白位置
        for ch in para:
            step = width_of(ch)
            if current and used + step > max_width:
                if space_at > 0 and is_word_char(ch) and is_word_char(current[-1]):
                    out.append(current[:space_at].rstrip())
                    current = current[space_at + 1:]
                    used = sum(width_of(c) for c in current)
                else:
                    out.append(current.rstrip())
                    current, used = "", 0.0
                space_at = -1
            current += ch
            used += step
            if ch == " ":
                space_at = len(current) - 1
        out.append(current.rstrip())    # 行尾空白會讓置中／靠右對齊偏掉一個空格
    return out


def _tinted(image: Image.Image, color: Color) -> Image.Image:
    """把 CColor 乘進圖片的 RGB（alpha 不變）。"""
    if (color.r, color.g, color.b) == (255, 255, 255):
        return image
    arr = np.asarray(image.convert("RGBA"), dtype=np.float32)
    arr[..., 0] *= color.r / 255.0
    arr[..., 1] *= color.g / 255.0
    arr[..., 2] *= color.b / 255.0
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def _quad_rect(quad: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _parse_color(text: str) -> tuple[int, int, int, int]:
    value = text.strip()
    named = {
        "black": (0, 0, 0, 255), "white": (255, 255, 255, 255),
        "gray": (128, 128, 128, 255), "grey": (128, 128, 128, 255),
        "magenta": (255, 0, 255, 255), "transparent": (0, 0, 0, 0),
    }
    if value.lower() in named:
        return named[value.lower()]
    value = value.lstrip("#")
    try:
        if len(value) == 3:
            return tuple(int(c * 2, 16) for c in value) + (255,)     # type: ignore[return-value]
        if len(value) == 6:
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4)) + (255,)  # type: ignore
        if len(value) == 8:
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4, 6))        # type: ignore
    except ValueError:
        pass
    return (0, 0, 0, 255)


# ---------------------------------------------------------------- 便利入口

def render_file(csd_path: str, frame: float = 0.0, animation: str | None = None,
                root: str | None = None, options: RenderOptions | None = None,
                assets: AssetLibrary | None = None) -> tuple[Document, RenderResult]:
    """讀檔 + 渲染的一站式入口。"""
    from .assets import library_for
    doc = parser.parse(csd_path)
    library = assets or library_for(csd_path, root)
    renderer = Renderer(library, options)

    frame_value = frame
    if animation:
        start, _end = anim.resolve_range(doc, animation)
        frame_value = frame + start if frame >= 0 else start
    return doc, renderer.render(doc, frame_value, animation)
