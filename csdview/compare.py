"""畫面比對：把 .csd 的渲染結果跟另一張圖（例如 Cocos Creator 的截圖）對照。

這是移植驗證的主力工具。除了整體差異數字，還會做兩件對 agent 特別有用的事：
1. 用相位相關估計整張圖的位移量 —— 移植時最常見的錯誤就是整體差一個偏移。
2. 逐節點算出各自區域的差異，直接指出「哪一個節點畫錯了」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from .render import NodeReport, RenderResult


# ---------------------------------------------------------------- 基本工具

def _to_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0


def _flatten(arr: np.ndarray, background: tuple[float, float, float]) -> np.ndarray:
    """把 RGBA 疊到指定底色上，避免透明區域的 RGB 亂數影響比對。"""
    alpha = arr[..., 3:4]
    bg = np.array(background, dtype=np.float32).reshape(1, 1, 3)
    return arr[..., :3] * alpha + bg * (1.0 - alpha)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _phase_correlate(a: np.ndarray, b: np.ndarray) -> tuple[int, int, float]:
    """相位相關本體：回傳把 b 平移多少可對齊 a。"""
    height, width = a.shape
    # 加漢寧窗降低邊界造成的假峰值
    window = np.outer(np.hanning(height), np.hanning(width)).astype(np.float32)
    fa = np.fft.rfft2((a - a.mean()) * window)
    fb = np.fft.rfft2((b - b.mean()) * window)
    cross = fa * np.conj(fb)
    magnitude = np.abs(cross)
    cross /= np.where(magnitude > 1e-9, magnitude, 1.0)
    surface = np.fft.irfft2(cross, s=a.shape)

    peak = int(np.argmax(surface))
    dy, dx = divmod(peak, width)
    if dy > height // 2:
        dy -= height
    if dx > width // 2:
        dx -= width
    confidence = float(surface.max() / (surface.std() + 1e-9))
    return (int(dx), int(dy), confidence)


def _box_downsample(arr: np.ndarray, step: int) -> np.ndarray:
    """區塊平均縮小。不能用跨步抽點——那會嚴重疊頻，高頻內容的相關性會整個消失。"""
    height, width = arr.shape
    h2, w2 = height // step, width // step
    trimmed = arr[:h2 * step, :w2 * step]
    return trimmed.reshape(h2, step, w2, step).mean(axis=(1, 3))


def _refine_shift(a: np.ndarray, b: np.ndarray, dx: int, dy: int,
                  radius: int, window: int = 1200) -> tuple[int, int]:
    """在粗估值附近逐格試，把降採樣造成的誤差補回來。

    只比對畫面中央一塊固定大小的區域，成本與整張圖無關。
    """
    height, width = a.shape
    ch, cw = min(height, window), min(width, window)
    y0, x0 = (height - ch) // 2, (width - cw) // 2
    patch = a[y0:y0 + ch, x0:x0 + cw]
    if float(patch.std()) < 1e-4:
        return (dx, dy)          # 中央是空白，微調沒有意義

    best_score, best = None, (dx, dy)
    for ody in range(dy - radius, dy + radius + 1):
        for odx in range(dx - radius, dx + radius + 1):
            sy, sx = y0 - ody, x0 - odx
            if sy < 0 or sx < 0 or sy + ch > height or sx + cw > width:
                continue
            score = float(np.abs(patch - b[sy:sy + ch, sx:sx + cw]).mean())
            if best_score is None or score < best_score:
                best_score, best = score, (odx, ody)
    return best


def estimate_shift(a: np.ndarray, b: np.ndarray,
                   max_pixels: int = 4_000_000) -> tuple[int, int, float]:
    """相位相關：估計要把 b 平移多少才能對齊 a。回傳 (dx, dy, 信心值)。

    整張圖直接做 FFT 在 16M 像素時要 5 秒以上，比對大型檔案會明顯卡住。
    改成先降採樣求粗值，再回到全解析度做小範圍微調，速度快很多而結果一樣精確。
    """
    height, width = a.shape
    if height < 8 or width < 8:
        return (0, 0, 0.0)

    step = 1
    while step < 4 and (height // (step + 1)) * (width // (step + 1)) >= max_pixels:
        step += 1

    if step == 1:
        return _phase_correlate(a, b)

    dx, dy, confidence = _phase_correlate(_box_downsample(a, step),
                                          _box_downsample(b, step))
    # 粗估誤差最多一個 step，回全解析度在 ±step 範圍內逐格找回精確值
    dx, dy = _refine_shift(a, b, dx * step, dy * step, step)
    return (dx, dy, confidence)


# ---------------------------------------------------------------- 結果結構

@dataclass
class RegionDiff:
    name: str
    path: str
    ctype: str
    rect: tuple[float, float, float, float]
    mae: float
    max_diff: float
    coverage: float
    is_leaf: bool
    kind: str = "content"       # content / text / particle / placeholder

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path,
            "type": self.ctype.replace("ObjectData", ""),
            "kind": self.kind,
            "screenRect": [round(v, 1) for v in self.rect],
            "meanDiff": round(self.mae, 4),
            "maxDiff": round(self.max_diff, 4),
            "diffPixelRatio": round(self.coverage, 4),
            "isLeaf": self.is_leaf,
        }


@dataclass
class CompareResult:
    size: tuple[int, int]
    reference_size: tuple[int, int]
    resized: bool
    mae: float
    rmse: float
    psnr: float
    diff_ratio: float
    alpha_iou: float
    shift: tuple[int, int]
    shift_confidence: float
    # 以下都有預設值，必須排在無預設值的欄位之後
    aspect_delta: float = 0.0     # 長寬比落差；縮放對齊時比例不同會扭曲並虛增差異
    regions: list[RegionDiff] = field(default_factory=list)
    # 把整體位移補正後再比一次：用來區分「整張圖偏移」與「單一節點畫錯」
    aligned_mae: float = 0.0
    aligned_diff_ratio: float = 0.0
    aligned_regions: list[RegionDiff] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    image: Image.Image | None = None

    @property
    def shifted(self) -> bool:
        return self.shift != (0, 0) and self.shift_confidence > 20

    @property
    def verdict(self) -> str:
        if self.diff_ratio < 0.01 and self.mae < 0.01:
            return "match"
        if self.shifted and self.aligned_diff_ratio < 0.02:
            return "shifted"          # 內容一致，只差整體位移
        if self.diff_ratio < 0.08:
            return "close"
        return "different"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "renderSize": list(self.size),
            "referenceSize": list(self.reference_size),
            "referenceResized": self.resized,
            "referenceAspectDelta": round(self.aspect_delta, 4),
            "meanDiff": round(self.mae, 5),
            "rmse": round(self.rmse, 5),
            "psnr": round(self.psnr, 2),
            "diffPixelRatio": round(self.diff_ratio, 5),
            "alphaIoU": round(self.alpha_iou, 4),
            "shiftToAlignReference": list(self.shift),
            "shiftConfidence": round(self.shift_confidence, 2),
            "afterShiftCompensation": {
                "meanDiff": round(self.aligned_mae, 5),
                "diffPixelRatio": round(self.aligned_diff_ratio, 5),
                "worstRegions": [r.to_dict() for r in self.aligned_regions],
            },
            "worstRegions": [r.to_dict() for r in self.regions],
            "hints": self.hints,
        }


def _hints(outcome: "CompareResult", missing: list[str],
           has_particles: bool) -> list[str]:
    """依實際數字產生判讀建議。

    這些結論呼叫端本來就能自己推，但推的過程容易漏掉前提——
    例如取景範圍不一致時，其他數字根本不該拿來下判斷。
    由工具算好放進輸出，判斷就不會因為忘記某一步而出錯。順序即重要性。

    自相矛盾的建議比沒有建議更糟，所以前提一旦失效就直接停止，
    不再依據不可信的數字往下推。
    """
    out: list[str] = []

    # 前提失效：其他數字都不該用來下判斷，講完就停
    if outcome.aspect_delta > 0.02:
        out.append(
            f"兩張圖長寬比差 {outcome.aspect_delta * 100:.0f}%，取景範圍不一致。"
            "縮放對齊會扭曲畫面，其餘差異數字都不可信。"
            "請重新截取涵蓋相同範圍的畫面再比一次，先不要修改版面。")
        return out
    if missing:
        out.append(
            f"渲染時有 {len(missing)} 個資源找不到，基準圖本身就不完整，"
            "差異可能來自缺圖而非移植錯誤。請先跑 doctor 補齊資源再比對。")
        return out

    if outcome.verdict == "match":
        out.append("畫面一致，這一格通過。")
        return out

    # 形狀完全吻合、顏色卻到處都不一樣 —— 那是背景不同，不是版面做錯
    if outcome.alpha_iou >= 0.99 and outcome.diff_ratio >= 0.10:
        out.append(
            f"每個元件的形狀與位置都吻合（alphaIoU {outcome.alpha_iou:.2f}），"
            f"但有 {outcome.diff_ratio * 100:.0f}% 的像素顏色不同——這種組合幾乎都是"
            "兩張圖的底色不一樣，不是版面問題。"
            "最常見的原因是拿 csdview 自己的 render 當基準圖："
            "render 預設背景是 checker 格子，compare 預設在純黑上比，"
            "產生基準圖時請加 --bg none（或把 --diff-bg 設成與截圖相同的底色）。")
        return out

    # 扣掉整體位移後才是真正該看的差異
    if outcome.shifted:
        dx, dy = outcome.shift
        out.append(
            f"整張圖差一個位移 ({dx}, {dy})，扣掉之後差異從 "
            f"{outcome.diff_ratio * 100:.1f}% 降到 {outcome.aligned_diff_ratio * 100:.1f}%。"
            "這是單一的錨點／Canvas 對齊／設計解析度設定問題，"
            "不是每個元件都擺錯，改一處即可。")
        if outcome.aligned_diff_ratio < 0.02:
            out.append("扣掉位移之後沒有其他明顯差異，修正對齊即可通過。")
            return out

    # 只看差異夠明顯的區域；殘留幾個百分點的多半是抗鋸齒或邊界，不值得追
    regions = [r for r in (outcome.aligned_regions if outcome.shifted
                           else outcome.regions) if r.coverage >= 0.05]
    real = [r for r in regions if r.kind == "content"]
    text = [r for r in regions if r.kind == "text"]
    noise = [r for r in regions if r.kind in ("particle", "placeholder")]

    if noise:
        kinds = {r.kind for r in noise}
        label = "粒子標示框" if "particle" in kinds else "內建佔位圖"
        out.append(
            f"差異最大的節點裡有 {len(noise)} 個是{label}，那不是實際內容"
            + ("，比對時加 --no-particles 排除。" if "particle" in kinds else "。"))

    if real:
        names = "、".join(r.path for r in real[:3])
        out.append(
            f"需要追查的是這 {len(real)} 個非文字節點，差異最大的是：{names}。"
            "用 spec 或 find 取它們的 screenRect，對照 Creator 端的實際位置與貼圖。")
    elif text:
        out.append(
            f"差異都集中在 {len(text)} 個文字節點。中文字寬誤差約 0%、"
            "純英數會偏寬約 24%，這屬於字型差異而非移植錯誤。"
            "請改看文字的位置是否正確；整段文字位移才是真問題。")
    elif not noise:
        out.append(
            "差異沒有集中在任何單一節點，比較像是整體性的問題"
            "（背景色、縮放倍率或截圖範圍）。先確認 --diff-bg 與截圖底色一致。")

    if has_particles and not noise:
        out.append("畫面含粒子節點（只畫標示框）。若差異數字偏高可加 --no-particles 重測。")
    return out


# ---------------------------------------------------------------- 主流程

def compare(result: RenderResult, reference: Image.Image,
            threshold: float = 0.05, background: str = "#000000",
            leaf_only: bool = True, min_area: int = 64,
            top: int = 15, make_image: bool = True) -> CompareResult:
    """比對渲染結果與參考圖。"""
    render_image = result.image
    ref_size = reference.size
    resized = False
    aspect_delta = 0.0
    if reference.size != render_image.size:
        # 內容範圍截圖幾乎不會與渲染尺寸完全相同，縮放對齊是必要的；
        # 但長寬比不同就會扭曲，差異數字會失真，所以把落差一併回報出去
        ra = render_image.size[0] / max(1, render_image.size[1])
        rb = ref_size[0] / max(1, ref_size[1])
        aspect_delta = abs(ra - rb) / max(ra, rb)
        reference = reference.resize(render_image.size, Image.LANCZOS)
        resized = True

    bg = _parse_bg(background)
    a = _flatten(_to_array(render_image), bg)
    b = _flatten(_to_array(reference), bg)

    delta = np.abs(a - b)
    per_pixel = delta.max(axis=2)
    mae = float(delta.mean())
    rmse = float(np.sqrt((delta ** 2).mean()))
    psnr = float(20 * np.log10(1.0 / rmse)) if rmse > 1e-9 else 99.0
    diff_ratio = float((per_pixel > threshold).mean())

    # 只看有沒有畫東西（形狀對不對），跟顏色無關
    alpha_a = np.asarray(render_image.convert("RGBA"), dtype=np.float32)[..., 3] > 8
    alpha_b = np.asarray(reference.convert("RGBA"), dtype=np.float32)[..., 3] > 8
    union = float((alpha_a | alpha_b).sum())
    alpha_iou = float((alpha_a & alpha_b).sum() / union) if union > 0 else 1.0

    dx, dy, confidence = estimate_shift(_luma(a), _luma(b))

    regions = _region_diffs(result.nodes, per_pixel, threshold,
                            leaf_only, min_area, top)

    # 補正整體位移後再比一次。位移被抵消掉之後，剩下的差異才是真正畫錯的地方。
    aligned_mae, aligned_ratio = mae, diff_ratio
    aligned_regions = regions
    if (dx, dy) != (0, 0) and confidence > 20 and abs(dx) < a.shape[1] // 4 \
            and abs(dy) < a.shape[0] // 4:
        shifted = np.roll(np.roll(b, dy, axis=0), dx, axis=1)
        valid = np.ones(a.shape[:2], dtype=bool)     # 捲動繞回來的邊界不列入計算
        if dy > 0:
            valid[:dy, :] = False
        elif dy < 0:
            valid[dy:, :] = False
        if dx > 0:
            valid[:, :dx] = False
        elif dx < 0:
            valid[:, dx:] = False

        aligned_delta = np.abs(a - shifted).max(axis=2)
        aligned_mae = float(np.abs(a - shifted).mean(axis=2)[valid].mean())
        aligned_ratio = float((aligned_delta[valid] > threshold).mean())
        masked = np.where(valid, aligned_delta, 0.0)
        aligned_regions = _region_diffs(result.nodes, masked, threshold,
                                        leaf_only, min_area, top)

    image = _diff_image(render_image, reference, per_pixel, threshold) if make_image else None

    outcome = CompareResult(
        size=render_image.size, reference_size=ref_size, resized=resized,
        aspect_delta=aspect_delta,
        mae=mae, rmse=rmse, psnr=psnr, diff_ratio=diff_ratio, alpha_iou=alpha_iou,
        shift=(dx, dy), shift_confidence=confidence, regions=regions,
        aligned_mae=aligned_mae, aligned_diff_ratio=aligned_ratio,
        aligned_regions=aligned_regions, image=image)
    outcome.hints = _hints(
        outcome, result.missing,
        any("Particle" in n.ctype for n in result.nodes if n.visible))
    return outcome


def _classify(node: NodeReport) -> str:
    """判斷這個節點的差異該怎麼看待。

    工具自己知道節點型別與資源來源，直接標出來，呼叫端就不必自己交叉比對
    「這是不是文字節點」才能套用判斷準則。
    """
    ctype = node.ctype
    if "Particle" in ctype:
        return "particle"        # 只畫標示框，差異沒有意義
    if (node.source or "").replace(chr(92), "/").startswith("Default/"):
        return "placeholder"     # Cocos Studio 內建佔位圖，本來就沒有實際資源
    if "Text" in ctype:
        return "text"            # 純英數字寬會偏寬約 24%，屬預期差異
    return "content"             # 真正該追查的差異


def _region_diffs(nodes: list[NodeReport], per_pixel: np.ndarray, threshold: float,
                  leaf_only: bool, min_area: int, top: int) -> list[RegionDiff]:
    height, width = per_pixel.shape
    out: list[RegionDiff] = []
    for node in nodes:
        if not node.visible:
            continue
        is_leaf = not node.children
        if leaf_only and not is_leaf:
            continue
        x, y, w, h = node.rect
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(width, int(x + w) + 1), min(height, int(y + h) + 1)
        if x1 - x0 < 1 or y1 - y0 < 1 or (x1 - x0) * (y1 - y0) < min_area:
            continue
        patch = per_pixel[y0:y1, x0:x1]
        out.append(RegionDiff(
            name=node.name, path=node.path, ctype=node.ctype,
            rect=(x, y, w, h),
            mae=float(patch.mean()), max_diff=float(patch.max()),
            coverage=float((patch > threshold).mean()), is_leaf=is_leaf,
            kind=_classify(node),
        ))

    out.sort(key=lambda r: (r.coverage, r.mae), reverse=True)
    return out[:top]


def _diff_image(render: Image.Image, reference: Image.Image,
                per_pixel: np.ndarray, threshold: float) -> Image.Image:
    """輸出三格對照圖：渲染 / 參考 / 差異熱區。"""
    width, height = render.size
    heat = np.zeros((height, width, 4), dtype=np.uint8)
    normalized = np.clip(per_pixel / max(threshold * 4, 1e-6), 0, 1)
    mask = per_pixel > threshold
    heat[..., 0] = (normalized * 255).astype(np.uint8)
    heat[..., 1] = ((1 - normalized) * 90).astype(np.uint8)
    heat[..., 3] = np.where(mask, 220, 0).astype(np.uint8)

    overlay = render.convert("RGBA").copy()
    grey = overlay.convert("L").convert("RGBA")
    grey.alpha_composite(Image.fromarray(heat, "RGBA"))

    gap, label = 8, 20
    sheet = Image.new("RGBA", (width * 3 + gap * 2, height + label), (22, 22, 26, 255))
    for index, (panel, title) in enumerate(
            [(render, "csd 渲染"), (reference, "參考圖"), (grey, "差異熱區")]):
        sheet.alpha_composite(panel.convert("RGBA"), (index * (width + gap), label))
    draw = ImageDraw.Draw(sheet)
    for index, title in enumerate(["csd render", "reference", "diff"]):
        draw.text((index * (width + gap) + 4, 4), title, fill=(150, 200, 255, 255))
    return sheet


def _parse_bg(text: str) -> tuple[float, float, float]:
    from .render import _parse_color
    r, g, b, _a = _parse_color(text)
    return (r / 255.0, g / 255.0, b / 255.0)
