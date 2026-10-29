"""資源解析：專案根目錄偵測、圖檔／plist 圖集／點陣字型／系統字型。

Cocos Studio 的 FileData.Path 都是相對於專案的 cocosstudio 資料夾，
所以先把根目錄找出來，之後所有路徑都從那裡展開。
"""
from __future__ import annotations

import os
import plistlib
import threading
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from PIL import Image, ImageFont

from .cache import memoize
from .model import FileRef

# 掃描時跳過的目錄：只列版控與編輯器／套件快取，絕不碰 build、frameworks 這類
# 有可能真的放著資源的地方——寧可慢一點，也不要靜默漏圖。
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".vs", ".vscode", ".idea", ".settings", ".github", ".gradle", ".venv",
})

# 救援索引只收得上用場的資源類型，字典小很多
ASSET_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".plist", ".fnt", ".ttf", ".ttc", ".otf",
    ".csd", ".csb", ".json",
})


def walk_pruned(base: str):
    """os.walk 但跳過版控／編輯器目錄。"""
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        yield dirpath, dirs, files


# ---------------------------------------------------------------- 圖集定義（.csi）

_ATLAS: dict[str, dict[str, str]] = {}
_ATLAS_LOCK = threading.Lock()


def atlas_map(root: str) -> dict[str, str]:
    """掃專案裡的 .csi 圖集定義，回傳「圖片相對路徑 -> 應該指向的 plist」。

    `.csi` 是 Cocos Studio 的圖集定義檔，`<ImageFiles>` 列出被打包進去的圖，
    產生的 plist 就放在它旁邊、同名。被打包的圖在編輯器的資源樹裡屬於那個 plist 群組，
    不再是獨立檔案，所以 `.csd` 必須寫 `Type="MarkedSubImage"` 並指定 `Plist`。
    寫成 `Type="Normal"` 的話 csdview 照樣讀得到磁碟上的散圖、渲染完全正常，
    但在 Cocos Studio 裡開起來會整片掉圖——這張表就是為了驗出那種檔。

    鍵一律小寫、正斜線，比對時不必再處理大小寫與分隔符的差異。
    """
    def build() -> dict[str, str]:
        table: dict[str, str] = {}
        for dirpath, _dirs, files in walk_pruned(root):
            for name in files:
                if not name.lower().endswith(".csi"):
                    continue
                try:
                    tree = ET.parse(os.path.join(dirpath, name))
                except (ET.ParseError, OSError):
                    continue          # 壞掉的圖集定義不該讓整個檢查失敗
                group = tree.getroot().find("PropertyGroup")
                atlas = (group.get("Name") if group is not None else "") \
                    or os.path.splitext(name)[0]
                folder = os.path.relpath(dirpath, root).replace(os.sep, "/")
                folder = "" if folder == "." else folder + "/"
                plist = folder + atlas + ".plist"
                for item in tree.getroot().iter("FilePathData"):
                    ref = (item.get("Path") or "").replace("\\", "/")
                    if ref:
                        table[ref.lower()] = plist
        return table

    key = os.path.normcase(os.path.abspath(root))
    return memoize(_ATLAS, key, _ATLAS_LOCK, build)

# ---------------------------------------------------------------- 根目錄偵測


def find_project_root(csd_path: str) -> str:
    """從 .csd 檔往上找專案根目錄（FileData.Path 的基準點）。

    依序嘗試：名為 cocosstudio 的祖先資料夾 → 含 .ccs 專案檔的資料夾 → 檔案自身目錄。
    """
    path = os.path.abspath(csd_path)
    directory = path if os.path.isdir(path) else os.path.dirname(path)

    current = directory
    while True:
        if os.path.basename(current).lower() == "cocosstudio":
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    current = directory
    while True:
        try:
            entries = os.listdir(current)
        except OSError:
            entries = []
        if any(e.lower().endswith(".ccs") for e in entries):
            nested = os.path.join(current, "cocosstudio")
            return nested if os.path.isdir(nested) else current
        parent = os.path.dirname(current)
        if parent == current:
            return directory
        current = parent


# ---------------------------------------------------------------- plist 圖集

_RECT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(m.group()) for m in _RECT_RE.finditer(text or "")]


@dataclass
class AtlasFrame:
    rect: tuple[int, int, int, int]          # 圖集中的 x, y, w, h
    rotated: bool = False
    offset: tuple[float, float] = (0.0, 0.0)
    source_size: tuple[int, int] = (0, 0)


class Atlas:
    """一個 .plist 圖集加上它的貼圖。"""

    def __init__(self, plist_path: str):
        self.path = plist_path
        self.frames: dict[str, AtlasFrame] = {}
        self.texture_path = ""
        self._texture: Image.Image | None = None
        self._load()

    def _load(self) -> None:
        with open(self.path, "rb") as fh:
            data = plistlib.load(fh)

        meta = data.get("metadata", {}) or {}
        tex_name = meta.get("textureFileName") or meta.get("realTextureFileName") or ""
        if not tex_name:
            tex_name = os.path.splitext(os.path.basename(self.path))[0] + ".png"
        self.texture_path = os.path.join(os.path.dirname(self.path), tex_name)

        for name, info in (data.get("frames", {}) or {}).items():
            key = name.replace("\\", "/")
            # 三種 plist 格式的欄位名稱完全不同：
            #   format 2 : frame / rotated / offset / sourceSize
            #   format 3 : textureRect / textureRotated / spriteOffset / spriteSourceSize
            #   format 0/1: x, y, width, height / offsetX, offsetY / originalWidth, ...
            rect_text = info.get("frame") or info.get("textureRect")
            if rect_text:
                nums = _numbers(rect_text)
                if len(nums) < 4:
                    continue
                rect = (int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3]))
                rotated = bool(info.get("rotated", info.get("textureRotated", False)))
                off = _numbers(info.get("offset") or info.get("spriteOffset") or "{0,0}")
                src = _numbers(info.get("sourceSize")
                               or info.get("spriteSourceSize") or "{0,0}")
            else:                                     # format 0 / 1
                rect = (int(info.get("x", 0)), int(info.get("y", 0)),
                        int(info.get("width", 0)), int(info.get("height", 0)))
                rotated = bool(info.get("textureRotated", False))
                off = [float(info.get("offsetX", 0)), float(info.get("offsetY", 0))]
                src = [float(info.get("originalWidth", 0)), float(info.get("originalHeight", 0))]
            if rect[2] <= 0 or rect[3] <= 0:
                continue

            self.frames[key] = AtlasFrame(
                rect=rect,
                rotated=rotated,
                offset=(off[0] if off else 0.0, off[1] if len(off) > 1 else 0.0),
                source_size=(int(src[0]) if src else rect[2],
                             int(src[1]) if len(src) > 1 else rect[3]),
            )

    def texture(self) -> Image.Image | None:
        if self._texture is None and os.path.isfile(self.texture_path):
            self._texture = Image.open(self.texture_path).convert("RGBA")
        return self._texture

    def sub_image(self, name: str) -> Image.Image | None:
        frame = self.frames.get(name.replace("\\", "/"))
        if frame is None:
            frame = self.frames.get(os.path.basename(name))
        tex = self.texture()
        if frame is None or tex is None:
            return None

        x, y, w, h = frame.rect
        crop = tex.crop((x, y, x + w, y + h))
        if frame.rotated:
            # 逆時針轉 90 度轉回直立。方向是實測確認的：取專案裡含文字的旋轉子圖
            # （如 HonorWall_empty、btn_autoPlay_n）兩種方向都畫出來比對，
            # 只有 +90 CCW 的中文與英文是正立可讀的。
            crop = crop.rotate(90, expand=True)

        # 還原被裁掉的透明邊，否則位置會偏
        sw, sh = frame.source_size
        cw, ch = crop.size
        if sw > 0 and sh > 0 and (sw, sh) != (cw, ch):
            canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            ox = int(round((sw - cw) / 2 + frame.offset[0]))
            oy = int(round((sh - ch) / 2 - frame.offset[1]))
            canvas.alpha_composite(crop, (max(0, ox), max(0, oy)))
            crop = canvas
        return crop


# ---------------------------------------------------------------- 點陣字型

@dataclass
class BMGlyph:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    xoffset: int = 0
    yoffset: int = 0
    xadvance: int = 0
    page: int = 0


@dataclass
class BMFont:
    line_height: int = 0
    base: int = 0
    glyphs: dict[int, BMGlyph] = field(default_factory=dict)
    kernings: dict[tuple[int, int], int] = field(default_factory=dict)
    pages: dict[int, Image.Image] = field(default_factory=dict)


# 允許等號前後有空白；值不吃掉下一個鍵（所以排除未加引號的等號）
_FNT_FIELD = re.compile(r'([A-Za-z_]+)\s*=\s*("[^"]*"|[^\s=]+)')


def load_bmfont(fnt_path: str) -> BMFont | None:
    """讀 AngelCode .fnt（文字格式）與它的貼圖頁。"""
    if not os.path.isfile(fnt_path):
        return None

    font = BMFont()
    base_dir = os.path.dirname(fnt_path)
    with open(fnt_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.split()
            if not parts:
                continue
            kind = parts[0]
            # 不能用「空白切 token 再切等號」：有些工具會把等號後面補空白對齊
            # （例如 `char id= 32   x=0`），那樣 id 的值會變成空字串，
            # 整個字型的字元全部被記成 id=0，點陣字就一個都畫不出來。
            fields: dict[str, str] = {
                key: value.strip('"')
                for key, value in _FNT_FIELD.findall(line[len(kind):])
            }

            def num(key, default=0):
                try:
                    return int(float(fields.get(key, default)))
                except ValueError:
                    return default

            if kind == "common":
                font.line_height = num("lineHeight")
                font.base = num("base")
            elif kind == "page":
                page_file = fields.get("file", "")
                full = os.path.join(base_dir, page_file)
                if os.path.isfile(full):
                    font.pages[num("id")] = Image.open(full).convert("RGBA")
            elif kind == "char":
                font.glyphs[num("id")] = BMGlyph(
                    x=num("x"), y=num("y"), w=num("width"), h=num("height"),
                    xoffset=num("xoffset"), yoffset=num("yoffset"),
                    xadvance=num("xadvance"), page=num("page"),
                )
            elif kind == "kerning":
                font.kernings[(num("first"), num("second"))] = num("amount")
    return font


# ---------------------------------------------------------------- 系統字型

_FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"),
    os.path.expanduser("~/AppData/Local/Microsoft/Windows/Fonts"),
    "/usr/share/fonts", "/Library/Fonts", "/System/Library/Fonts",
]

# 依序挑第一個存在的，優先中文字型（多數 .csd 的文字是中文）
_FALLBACK_FONTS = [
    "msjh.ttc", "msyh.ttc", "mingliu.ttc", "simhei.ttf", "simsun.ttc",
    "NotoSansCJK-Regular.ttc", "PingFang.ttc", "DejaVuSans.ttf", "arial.ttf",
]


def _font_file_in_dirs(filename: str) -> str | None:
    for directory in _FONT_DIRS:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def system_font_path(font_name: str = "") -> str | None:
    """把字型名稱（可能是中文名或檔名）解析成實際檔案路徑。"""
    if font_name:
        name = font_name.strip()
        if os.path.isfile(name):
            return name
        for ext in ("", ".ttf", ".ttc", ".otf", ".TTF"):
            found = _font_file_in_dirs(name + ext)
            if found:
                return found
        alias = {
            "微軟正黑體": "msjh.ttc", "Microsoft JhengHei": "msjh.ttc",
            "微软雅黑": "msyh.ttc", "Microsoft YaHei": "msyh.ttc",
            "新細明體": "mingliu.ttc", "PMingLiU": "mingliu.ttc",
            "細明體": "mingliu.ttc", "黑体": "simhei.ttf", "宋体": "simsun.ttc",
            "Arial": "arial.ttf",
        }.get(name)
        if alias:
            found = _font_file_in_dirs(alias)
            if found:
                return found

    for candidate in _FALLBACK_FONTS:
        found = _font_file_in_dirs(candidate)
        if found:
            return found
    return None


# ---------------------------------------------------------------- 內建佔位圖

_PLACEHOLDER_CACHE: dict[str, Image.Image] = {}


def _default_placeholder(path: str) -> Image.Image:
    """重現 Cocos Studio 內建的灰色預設圖（Button_Normal.png 之類）。"""
    from PIL import ImageDraw

    name = os.path.basename(path).lower()
    if name in _PLACEHOLDER_CACHE:
        return _PLACEHOLDER_CACHE[name]

    is_button = "button" in name
    size = (100, 40) if is_button else (72, 72)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = [0, 0, size[0] - 1, size[1] - 1]
    if is_button:
        draw.rounded_rectangle(box, radius=6, fill=(206, 206, 206, 255),
                               outline=(140, 140, 140, 255))
    else:
        draw.rectangle(box, fill=(200, 200, 200, 255), outline=(140, 140, 140, 255))
        draw.line([0, 0, size[0] - 1, size[1] - 1], fill=(170, 170, 170, 255))
        draw.line([0, size[1] - 1, size[0] - 1, 0], fill=(170, 170, 170, 255))
    _PLACEHOLDER_CACHE[name] = image
    return image


# ---------------------------------------------------------------- 資源庫

class AssetLibrary:
    """一個專案根目錄底下所有資源的快取與解析。"""

    def __init__(self, root: str, extra_roots: list[str] | None = None,
                 font_override: str | None = None):
        self.root = os.path.abspath(root)
        self.extra_roots = [os.path.abspath(p) for p in (extra_roots or [])]
        # 指定後所有文字都用這個字型，不理 .csd 裡寫的字型名稱。
        # 實測：中文用 msjh 與 Cocos Studio 的設計字寬中位數誤差 0%，
        # 但純西文偏寬約 24%（Arial Narrow 類的窄體才貼近）。要逐像素對齊
        # 西文時就用這個選項指定，否則維持預設即可。
        self.font_override = font_override or None
        self.missing: set[str] = set()
        self._images: dict[str, Image.Image | None] = {}
        self._atlases: dict[str, Atlas | None] = {}
        self._bmfonts: dict[str, BMFont | None] = {}
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self._index: dict[str, str] | None = None    # 檔名 -> 完整路徑（救援用）
        self._index_lock = threading.Lock()
        # 每個快取一把鎖：全部共用一把會讓平行暖機時的貼圖解碼變成序列。
        # FreeType 不是執行緒安全的，所以字型那把鎖是必要而非防禦性的。
        self._image_lock = threading.Lock()
        self._atlas_lock = threading.Lock()
        self._bmfont_lock = threading.Lock()
        self._font_lock = threading.Lock()

    # ---- 路徑
    def resolve(self, rel_path: str) -> str | None:
        """把專案相對路徑轉成磁碟上的實際路徑。"""
        if not rel_path:
            return None
        rel = rel_path.replace("\\", "/").lstrip("/")
        for base in [self.root, *self.extra_roots]:
            candidate = os.path.join(base, rel)
            if os.path.isfile(candidate):
                return candidate
        # 路徑對不上時，退而用檔名在整個專案裡找
        return self._lookup_by_name(os.path.basename(rel))

    def _lookup_by_name(self, filename: str) -> str | None:
        """用檔名在整個專案裡找檔案，給路徑對不上時救援用。

        索引要「建好才發布」：先前是先把空字典指給 self._index 再開始掃，
        網頁介面併發請求時，其他執行緒會讀到還沒填完的索引而誤判成缺資源，
        而且那個 None 會被 _images 快取住，整個 session 都不會恢復。
        """
        index = self._index
        if index is None:
            with self._index_lock:
                index = self._index
                if index is None:
                    built: dict[str, str] = {}
                    for base in [self.root, *self.extra_roots]:
                        for dirpath, _dirs, files in walk_pruned(base):
                            for name in files:
                                if os.path.splitext(name)[1].lower() in ASSET_EXTS:
                                    built.setdefault(name.lower(),
                                                     os.path.join(dirpath, name))
                    self._index = index = built
        return index.get(filename.lower())

    # ---- 圖片
    def image(self, ref: FileRef) -> Image.Image | None:
        """依 FileRef 取得圖片；先找鬆散檔案，找不到再從 plist 圖集拆。"""
        if not ref or not ref.path:
            return None
        key = f"{ref.path}|{ref.plist}"
        return memoize(self._images, key, self._image_lock,
                       lambda: self._load_image(ref), limit=256)

    def _load_image(self, ref: FileRef) -> Image.Image | None:
        img: Image.Image | None = None
        full = self.resolve(ref.path)
        if full:
            try:
                img = Image.open(full).convert("RGBA")
            except Exception:
                img = None

        if img is None and ref.plist:
            atlas = self.atlas(ref.plist)
            if atlas:
                img = atlas.sub_image(ref.path)

        if img is None and ref.type == "Default":
            # Cocos Studio 內建的預設佔位圖，本來就不在專案裡，不算缺資源
            img = _default_placeholder(ref.path)
        elif img is None:
            self.missing.add(ref.path)
        return img

    def atlas(self, plist_rel: str) -> Atlas | None:
        return memoize(self._atlases, plist_rel, self._atlas_lock,
                       lambda: self._load_atlas(plist_rel), limit=64)

    def _load_atlas(self, plist_rel: str) -> Atlas | None:
        result: Atlas | None = None
        full = self.resolve(plist_rel)
        if full:
            try:
                result = Atlas(full)
            except Exception:
                result = None
        if result is None:
            self.missing.add(plist_rel)
        return result

    # ---- 字型
    def bmfont(self, ref: FileRef) -> BMFont | None:
        if not ref or not ref.path:
            return None
        return memoize(self._bmfonts, ref.path, self._bmfont_lock,
                       lambda: self._load_bmfont(ref), limit=32)

    def _load_bmfont(self, ref: FileRef) -> BMFont | None:
        full = self.resolve(ref.path)
        result = load_bmfont(full) if full else None
        if result is None:
            self.missing.add(ref.path)
        return result

    def truetype(self, ref: FileRef, font_name: str, size: int) -> ImageFont.FreeTypeFont | None:
        """取得 TTF 字型物件；專案內字型優先，其次系統字型。"""
        size = max(1, int(size))
        path: str | None = None
        if self.font_override:
            path = self.resolve(self.font_override) or system_font_path(self.font_override)
        if path is None and ref and ref.path:
            path = self.resolve(ref.path)
            if path is None:
                self.missing.add(ref.path)
        if path is None:
            path = system_font_path(font_name)
        if path is None:
            return None

        def build():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                try:
                    return ImageFont.truetype(system_font_path() or "", size)
                except Exception:
                    return None

        return memoize(self._fonts, (path, size), self._font_lock, build, limit=64)


def library_for(csd_path: str, root: str | None = None,
                extra_roots: list[str] | None = None,
                font_override: str | None = None) -> AssetLibrary:
    return AssetLibrary(root or find_project_root(csd_path), extra_roots, font_override)


if __name__ == "__main__":  # 快速檢查根目錄偵測
    for arg in sys.argv[1:]:
        print(arg, "->", find_project_root(arg))
