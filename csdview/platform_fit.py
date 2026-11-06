"""系統字在 Windows／Android／iOS 寬度不同：換到別的平台會不會超出框。

沒指定字型（.csd 沒有 FontResource）的文字，執行時由引擎的 FontHelper 依平台挑字
（Mangan_git/Cocos2d-x/cocos/base/CCFontHelper.cpp）：
  Windows  Microsoft JhengHei（GDI）
  Android  引擎是 Noto Sans CJK TC（英數 Roboto）。要的空位跟 Windows 差不多，
           所以這裡沿用微軟正黑的字寬，不讀 Noto／Roboto。
  iOS      PingFang TC（一般字重交給 systemFontOfSize，英文數字是 San Francisco）
三個平台的字級意思相同（字身高度）。中文一律佔一個字寬。會跟 Windows 差開的是 iOS 的英數。

這裡用「各平台的字寬」判斷換平台會不會超框：
  - Windows 找得到微軟正黑才量；找不到就在 coverage 裡寫清楚，不假裝量過
  - Android 不偵測字型檔，字寬直接等於 Windows
  - iOS 不偵測字型檔。規則從 PingFang TC Regular 提煉後寫死（見 _IOS_ASCII_PCT）：
    漢字與全形剛好一個字寬；空白到 ~ 的每個字有自己的 em 比例。
    這包字沒有 San Francisco，英數用的是蘋方裡的英文字，不是系統的 SF Pro。
  - 中文字在找不到中文字型時算一個字寬（所有中文字型的漢字都是全形，這個近似很穩）
行高刻意不比：各平台行高取決於實際用到的主字型，Windows 上驗證不了。這裡沿用 csdview
渲染的行高模型（行距＝字級），只抓「換平台後字變寬 → 多換一行或單字塞不下 → 超框」，
所以結果跟 doctor 的 textOverflow 用同一把尺，只差在字寬。

Windows 的字型去系統字型資料夾、使用者字型資料夾，以及 CSDVIEW_FONT_DIR 找。
"""
from __future__ import annotations

import os
import threading

from PIL import ImageFont

from .assets import _FONT_DIRS
from .render import _wrap_lines

# Windows 才找字型檔。Android 空位跟 Windows 差不多，沿用同一份字寬。iOS 用寫死的蘋方規則。
_PLATFORMS = [
    {"key": "windows", "label": "Windows",
     "cjk": [("msjh.ttc", "Microsoft JhengHei")], "latin": []},
    {"key": "android", "label": "Android", "rule": "windows"},
    {"key": "ios", "label": "iOS", "rule": "pingfang"},
]


def _font_dirs() -> list[str]:
    dirs = []
    extra = os.environ.get("CSDVIEW_FONT_DIR")
    if extra:
        dirs += [d for d in extra.split(os.pathsep) if d]
    local = os.environ.get("LOCALAPPDATA")
    if local:                                     # Windows 10 起「只為我安裝」的字型
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    dirs.append(os.path.join(os.path.expanduser("~"), "Library", "Fonts"))
    return dirs + list(_FONT_DIRS)


def _find(candidates) -> tuple[str, int, str] | None:
    """(路徑, ttc 的第幾個字面, 顯示名稱)。face 有給時在 .ttc 裡找名字相符的那一面。"""
    for filename, face in candidates:
        for directory in _font_dirs():
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            index = 0
            if face and path.lower().endswith(".ttc"):
                index = -1
                for i in range(16):
                    try:
                        family, style = ImageFont.truetype(path, 12, index=i).getname()
                    except OSError:
                        break
                    if family == face and style in ("Regular", "W3", "Normal"):
                        index = i
                        break
                if index < 0:
                    continue
            return path, index, filename
    return None


class _Measure:
    """一個平台的量字器：逐字累加字寬（跟 csdview 斷行一樣的算法）。"""

    def __init__(self, cjk, latin, size: int):
        self.size = size
        self.cjk = self._load(cjk, size) if cjk else None
        self.latin = self._load(latin, size) if latin else None

    @staticmethod
    def _load(found, size):
        path, index, _name = found
        font = ImageFont.truetype(path, size, index=index)
        try:
            font.set_variation_by_name("Regular")    # 可變字型預設不一定是 Regular
        except Exception:
            pass
        return font

    def getlength(self, text: str) -> float:
        total = 0.0
        for ch in text:
            if _is_cjk(ch):
                total += self.cjk.getlength(ch) if self.cjk else self.size
            else:
                font = self.latin or self.cjk
                total += font.getlength(ch) if font else self.size * 0.55
        return total


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF or 0x3000 <= code <= 0x303F)


# 空白（U+0020）到 ~（U+007E）。單位是 em 的百分之一。
# 2026-11-06 用 PingFang TC Regular 在字級 100 量 getlength，再除以 100。
# 跟同包 TrueType Collection 裡的 PingFang TC Regular 一致。漢字與全形另計，都是 100。
_IOS_ASCII_PCT = bytes((
    33, 33, 43, 60, 60, 97, 73, 24, 33, 33, 51, 61, 26, 61, 26, 50,
    60, 40, 60, 60, 60, 60, 60, 55, 60, 60, 26, 26, 61, 61, 61, 54,
    86, 66, 68, 73, 71, 64, 58, 75, 72, 24, 52, 69, 59, 88, 72, 77,
    64, 77, 68, 63, 62, 71, 64, 93, 64, 66, 62, 33, 50, 33, 52, 50,
    33, 56, 59, 55, 59, 56, 37, 59, 56, 26, 27, 53, 24, 86, 56, 59,
    59, 59, 37, 51, 36, 56, 48, 76, 51, 50, 49, 33, 20, 33, 50,
))
# 不在上表、也不是漢字／全形的字。英數裡最常見的一檔是 0.60em。
_IOS_OTHER_PCT = 60


class _IosRule:
    """iOS 字寬。不讀字型檔，套上面那張蘋方規則。"""

    def __init__(self, size: int):
        self.size = size

    def getlength(self, text: str) -> float:
        total = 0.0
        for ch in text:
            if _is_cjk(ch):
                total += self.size
                continue
            code = ord(ch)
            if 32 <= code <= 126:
                pct = _IOS_ASCII_PCT[code - 32]
            else:
                pct = _IOS_OTHER_PCT
            total += self.size * pct / 100.0
        return total


class PlatformFonts:
    """找出這台電腦能量哪些平台。一個程序找一次就好（掃字型資料夾不便宜）。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.platforms = []
        for spec in _PLATFORMS:
            if spec.get("rule") == "pingfang":
                self.platforms.append({
                    **spec, "measurable": True,
                    "note": "蘋方規則，不讀字型檔：漢字與全形一個字寬；"
                            "英數與半形標點用 PingFang TC Regular 的字寬表",
                })
                continue
            if spec.get("rule") == "windows":
                win = next(p for p in self.platforms if p["key"] == "windows")
                note = ("空位跟 Windows 差不多，沿用微軟正黑的字寬，不讀 Noto／Roboto"
                        if win["measurable"] else "Windows 的字型也沒有，沒得沿用")
                self.platforms.append({**spec, "measurable": win["measurable"], "note": note})
                continue
            cjk = _find(spec["cjk"])
            latin = _find(spec["latin"])
            if not cjk and not latin:
                note = "沒有字型可量"
                self.platforms.append({**spec, "measurable": False, "note": note})
                continue
            parts = []
            if cjk:
                parts.append(f"中文 {cjk[2]}")
            else:
                parts.append("中文算一個字寬")
            if latin:
                parts.append(f"英數 {latin[2]}")
            self.platforms.append({**spec, "measurable": True, "cjkFont": cjk,
                                   "latinFont": latin, "note": "、".join(parts)})
        self._measures: dict[tuple[str, int], _Measure] = {}
        self._measure_lock = threading.Lock()

    @classmethod
    def get(cls) -> "PlatformFonts":
        with cls._lock:
            if cls._instance is None:
                cls._instance = PlatformFonts()
            return cls._instance

    def measure(self, key: str, size: int):
        spec = next((p for p in self.platforms if p["key"] == key), None)
        if spec is None or not spec["measurable"]:
            return None
        if spec.get("rule") == "windows":
            return self.measure("windows", size)
        size = max(1, int(size))
        with self._measure_lock:
            got = self._measures.get((key, size))
            if got is None:
                got = (_IosRule(size) if spec.get("rule") == "pingfang"
                       else _Measure(spec.get("cjkFont"), spec.get("latinFont"), size))
                self._measures[(key, size)] = got
            return got

    def coverage(self) -> dict:
        return {p["key"]: {"label": p["label"], "measured": p["measurable"], "note": p["note"]}
                for p in self.platforms}


# ---------------------------------------------------------------- 一個節點
def text_widths(text: str, size: int) -> dict[str, float | None]:
    """每個平台的單行寬（多段取最寬的那段）；量不到的是 None。"""
    fonts = PlatformFonts.get()
    out = {}
    for spec in fonts.platforms:
        m = fonts.measure(spec["key"], size)
        out[spec["key"]] = (max(m.getlength(line) for line in text.split("\n"))
                            if m is not None else None)
    return out


def box_fit(text: str, size: int, box_w: float, box_h: float,
            ink_height: float) -> dict[str, dict]:
    """固定框文字在每個平台要幾行、要多高多寬。行高模型同 csdview 的渲染（行距＝字級）。"""
    fonts = PlatformFonts.get()
    out = {}
    paragraphs = text.split("\n")
    for spec in fonts.platforms:
        m = fonts.measure(spec["key"], size)
        if m is None:
            continue
        lines = _wrap_lines(paragraphs, m, box_w)
        width = max((m.getlength(line) for line in lines), default=0.0)
        height = max(1, size) * (len(lines) - 1) + ink_height
        out[spec["key"]] = {"lines": len(lines), "width": width, "height": height,
                            "fits": width <= box_w + 2 and height <= box_h + 2}
    return out


# ---------------------------------------------------------------- 整份檔
_SYSTEM_FONT_TEXT = {"TextObjectData", "TextFieldObjectData"}
_VISUAL_PARENTS = {"ImageViewObjectData", "SpriteObjectData", "ButtonObjectData",
                   "PanelObjectData", "LoadingBarObjectData"}


def check_document(doc, result, renderer) -> list[dict]:
    """這份檔裡「Windows 放得下、換到別的平台會超出」的文字。

    三種「框」：
    - 固定框文字（IsCustomSize）：照各平台字寬重新斷行，行數變多或單字塞不下就超框
    - 自動大小的文字：擺在有底圖／底色的父層上時，以父層的寬為框（文字變寬就跑出底圖）
    - 按鈕標題：以按鈕的寬為框
    只看系統字（沒有 FontResource）；TTF／點陣字三個平台一樣，不會有這個問題。
    只看這份檔自己的節點，巢狀引用的 .csd 在它自己的檔裡檢查。
    """
    fonts = PlatformFonts.get()
    others = [p for p in fonts.platforms if p["key"] != "windows" and p["measurable"]]
    if not others:
        return []
    parent_of = {}
    stack = [result.root] if result.root else []
    while stack:
        report = stack.pop()
        for child in report.children:
            parent_of[id(child)] = report
            stack.append(child)

    issues = []
    for report in result.nodes:
        node = report.node
        if node is None or report.owner is not doc or not node.text.strip():
            continue
        if node.font_file and node.font_file.path:
            continue                                   # TTF／點陣字：各平台一樣
        size = max(1, int(node.font_size))
        if node.ctype in _SYSTEM_FONT_TEXT and node.custom_size and node.width > 0 and node.height > 0:
            ink = _ink_height(renderer, node, size)
            fit = box_fit(node.text, size, node.width, node.height, ink)
            win = fit.get("windows")
            if not win or not win["fits"]:
                continue                               # Windows 就超框：textOverflow 已經報了
            bad = {k: v for k, v in fit.items() if k != "windows" and not v["fits"]}
            if bad:
                issues.append({
                    "path": report.path, "kind": "box", "box": [node.width, node.height],
                    "windows": _round(win), "platforms": {k: _round(v) for k, v in bad.items()},
                })
        elif node.ctype in _SYSTEM_FONT_TEXT and not node.custom_size:
            parent = parent_of.get(id(report))
            frame = _parent_frame(parent)
            if frame is None or abs(node.rotation_x) > 0.01 or abs(node.rotation_y) > 0.01:
                continue
            widths = text_widths(node.text, size)
            spans = {k: _span(node, w) for k, w in widths.items() if w is not None}
            win = spans.get("windows")
            if win is None or not _inside(win, frame):
                continue                               # Windows 就已經超出父層：不是跨平台的問題
            bad = {k: s for k, s in spans.items() if k != "windows" and not _inside(s, frame)}
            if bad:
                issues.append({
                    "path": report.path, "kind": "parent", "parent": parent.path,
                    "frame": [round(frame[0], 1), round(frame[1], 1)],
                    "windows": {"width": round(widths["windows"], 1)},
                    "platforms": {k: {"width": round(widths[k], 1),
                                      "over": round(max(frame[0] - s[0], s[1] - frame[1]), 1)}
                                  for k, s in bad.items()},
                })
        elif node.ctype == "ButtonObjectData":
            button_w = report.size[0]
            if button_w <= 0:
                continue
            widths = text_widths(node.text, size)
            if widths.get("windows") is None or widths["windows"] > button_w + 1:
                continue
            bad = {k: w for k, w in widths.items()
                   if k != "windows" and w is not None and w > button_w + 1}
            if bad:
                issues.append({
                    "path": report.path, "kind": "button", "box": [round(button_w, 1)],
                    "windows": {"width": round(widths["windows"], 1)},
                    "platforms": {k: {"width": round(w, 1)} for k, w in bad.items()},
                })
    return issues


def _round(fit: dict) -> dict:
    return {"lines": fit["lines"], "width": round(fit["width"], 1),
            "height": round(fit["height"], 1)}


def _ink_height(renderer, node, size: int) -> float:
    """最後一行的墨跡高度，跟 csdview 畫文字時用的同一個字型。"""
    font = renderer.assets.truetype(node.font_file, node.font_name, size)
    try:
        ascent, descent = font.getmetrics()
        return float(max(1, ascent + descent))
    except (AttributeError, OSError):
        return size * 1.25


def _parent_frame(parent) -> tuple[float, float] | None:
    """父層在自己座標系裡的水平範圍 (左, 右)；父層沒有底圖／底色就不算框。"""
    if parent is None or parent.node is None:
        return None
    pnode = parent.node
    if pnode.ctype not in _VISUAL_PARENTS:
        return None
    has_visual = bool(parent.source) or pnode.ctype == "ButtonObjectData" or (
        pnode.ctype == "PanelObjectData" and pnode.back_color_enabled and pnode.back_color_alpha > 0)
    width = parent.size[0]
    if not has_visual or width <= 1:
        return None
    return (0.0, width)


def _span(node, width: float) -> tuple[float, float]:
    scale = abs(node.scale_x) or 1.0
    left = node.x - node.anchor_x * width * scale
    return (left, left + width * scale)


def _inside(span, frame, tolerance: float = 1.0) -> bool:
    return span[0] >= frame[0] - tolerance and span[1] <= frame[1] + tolerance


# ---------------------------------------------------------------- 寫入前擋下
# 新建或調整 .csd 時，框是照 csdview（Windows）排的。寫檔前用同一把尺看：
# Android 沿用 Windows 的字寬；iOS 用寫死的蘋方規則。放不下才不寫。
_LABEL = {"windows": "Windows", "android": "Android", "ios": "iOS"}


def _related(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def review_unsaved(csd_path: str, xml: bytes, roots: list[str]) -> dict:
    """用還沒寫入的 XML 看這次動到的系統字。不改檔。"""
    import tempfile
    folder = os.path.dirname(os.path.abspath(csd_path)) or "."
    fd, tmp = tempfile.mkstemp(prefix="._csdfit_", suffix=".csd", dir=folder)
    try:
        os.write(fd, xml)
        os.close(fd)
        fd = None
        return _review_file(tmp, roots)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _review_file(csd_path: str, roots: list[str]) -> dict:
    from .assets import library_for
    from .parser import parse
    from .render import RenderOptions, Renderer

    doc = parse(csd_path)
    renderer = Renderer(library_for(csd_path), RenderOptions(layout_only=True))
    result = renderer.render(doc, 0.0, "", {})
    return _gate(doc, result, renderer, roots)


def _gate(doc, result, renderer, roots: list[str]) -> dict:
    coverage = PlatformFonts.get().coverage()
    concerns = [
        {"path": report.path, "kind": "windows",
         "box": [round(v, 1) for v in report.size],
         "needs": [round(v, 1) for v in report.overflow]}
        for report in result.nodes
        if report.overflow and _related(report.path, roots)
    ]
    concerns.extend(
        item for item in check_document(doc, result, renderer)
        if _related(item["path"], roots)
    )
    _attach_suggest(concerns, result)
    return {
        "block": bool(concerns),
        "concerns": concerns,
        "fonts": coverage,
        "message": _block_message(concerns) if concerns else _clear_note(coverage),
        "waiveNote": _waive_note(concerns) if concerns else "",
    }


def _attach_suggest(concerns: list[dict], result) -> None:
    """每筆補上放得下的寬度：固定框是文字寬，父層是父層該有的寬。只用量得到的平台。"""
    by_path = {report.path: report for report in result.nodes}
    for item in concerns:
        if item["kind"] == "parent":
            frame = item.get("frame") or [0, 0]
            extra = max((got.get("over") or 0) for got in item["platforms"].values())
            item["suggestWidth"] = round((frame[1] - frame[0]) + extra, 1)
            continue
        report = by_path.get(item["path"])
        node = report.node if report is not None else None
        if node is None or not node.text.strip():
            continue
        widths = text_widths(node.text, max(1, int(node.font_size)))
        measured = [w for w in widths.values() if w]
        if measured:
            item["suggestWidth"] = round(max(measured), 1)


def _block_message(concerns: list[dict]) -> str:
    lines = ["這次改的系統字在其他平台放不下，檔案沒有寫入。", _detail(concerns),
             "把框加寬到上面的寬度，或縮短文案，然後再送一次。",
             "若這段字要在程式裡依該機字寬另外處理，加上 allowPlatformOverflow: true 再送一次，"
             "並告訴使用者哪個節點、打算怎麼處理。"
             "超過寬度就換行、最後一行補 ... 不是預設做法，要用要另外得到同意。"]
    return "\n".join(lines)


def _waive_note(concerns: list[dict]) -> str:
    return ("已寫入。這些節點在其他平台放不下，回覆時要告訴使用者哪個節點、打算怎麼處理：\n"
            + _detail(concerns))


def _detail(concerns: list[dict]) -> str:
    by_path: dict[str, list[dict]] = {}
    for item in concerns:
        by_path.setdefault(item["path"], []).append(item)
    lines = []
    for path, items in by_path.items():
        bits = []
        suggest = None
        for item in items:
            if item.get("suggestWidth"):
                suggest = max(suggest or 0, item["suggestWidth"])
            kind = item["kind"]
            if kind == "windows":
                box, needs = item["box"], item["needs"]
                bits.append(f"Windows 就要 {needs[0]}×{needs[1]}（框 {box[0]}×{box[1]}）")
            elif kind == "box":
                for key, got in item["platforms"].items():
                    label = _LABEL.get(key, key)
                    bits.append(f"{label} 要 {got['lines']} 行、高 {got['height']}"
                                f"（Windows {item['windows']['lines']} 行，框 {item['box'][0]}×{item['box'][1]}）")
            elif kind == "button":
                for key, got in item["platforms"].items():
                    label = _LABEL.get(key, key)
                    bits.append(f"{label} 寬 {got['width']}（按鈕寬 {item['box'][0]}，"
                                f"Windows {item['windows']['width']}）")
            elif kind == "parent":
                for key, got in item["platforms"].items():
                    label = _LABEL.get(key, key)
                    bits.append(f"{label} 超出父層 {got['over']}")
        if suggest:
            where = "父層加寬到" if any(item["kind"] == "parent" for item in items) else "加寬到"
            bits.append(f"{where} {suggest}")
        lines.append(f"{path}：" + "；".join(bits))
    return "\n".join(lines)


def _clear_note(coverage: dict) -> str:
    missing = [info["label"] for key, info in coverage.items()
               if key != "windows" and not info["measured"]]
    approx = [info["label"] for info in coverage.values() if "近似" in (info.get("note") or "")]
    text = "這次改的系統字，量得到的平台放得下。"
    if missing:
        text += f"{'、'.join(missing)} 沒量到，不擋寫檔。"
    if approx:
        text += f"{'、'.join(approx)} 的英數是近似值。"
    return text
