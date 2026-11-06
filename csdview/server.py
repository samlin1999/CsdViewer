"""本機網頁檢視器。

渲染完全在 Python 端做，瀏覽器只負責顯示 PNG 與控制時間軸，
所以人在網頁上看到的畫面，跟 agent 用 CLI 取得的圖片是同一份程式碼算出來的。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import threading
import time
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import anim, details, parser as csd_parser, scan
from .assets import SKIP_DIRS, AssetLibrary, library_for
from .render import NodeReport, RenderOptions, Renderer, RenderResult

_HERE = os.path.dirname(os.path.abspath(__file__))


def _stamp(path: str) -> dict:
    """檔案的版本指紋，用來判斷 .csd 有沒有被改過。

    刻意用奈秒而不是秒：MCP 或 agent 直接改檔時，一秒內連改好幾次是常態
    （實測連續六次原子寫入，取整秒的話前端只看得到一次），秒級精度會漏掉更新。
    再帶上檔案大小，即使檔案系統的時間戳精度不足也還有第二道依據。
    """
    st = os.stat(path)
    return {"mtime": st.st_mtime_ns, "size": st.st_size}


def _version(path: str) -> tuple[int, int]:
    """同一份指紋的 tuple 版本，給伺服器端的快取鍵用。"""
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


class _Cache:
    """給時間軸拖曳用的 LRU：同一格重複要求就直接回快取。

    同時限張數與總位元組。只限張數是不夠的：影格 PNG 的大小差很多
    （實測中位數 280 KB，大的到 919 KB），240 張全是大圖就吃掉 226 MB，
    而使用者只是把一段長動畫拖過去而已。以位元組為準佔用才可預測。
    """

    def __init__(self, limit: int = 240, limit_bytes: int = 128 * 1024 * 1024):
        self.limit = limit
        self.limit_bytes = limit_bytes
        self._data: OrderedDict[tuple, bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key, value) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._data[key] = value
            self._bytes += len(value)
            # 單張就超過上限時至少留著它，不然這一張永遠進不了快取
            while len(self._data) > 1 and (len(self._data) > self.limit
                                           or self._bytes > self.limit_bytes):
                _, dropped = self._data.popitem(last=False)
                self._bytes -= len(dropped)

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return len(self._data), self._bytes


class ViewerState:
    def __init__(self, root: str, project_root: str | None = None):
        # 影格圖片被標成可長期快取，所以網址一定要能反映「輸出可能已經改變」。
        # 只用 .csd 的 mtime 不夠：修好渲染程式或換掉貼圖／字型時 .csd 並沒有動，
        # 網址不變，瀏覽器就會一直沿用舊圖。加上這個開機序號，重啟伺服器
        # （載入新程式碼必經的動作）就會讓所有網址失效。
        self.boot = int(time.time())
        self.root = os.path.abspath(root)
        self.project_root = project_root
        self.cache = _Cache()
        self._docs: OrderedDict[str, tuple[tuple[int, int], object]] = OrderedDict()
        self._doc_limit = 64
        # 依「檔案 + 算圖選項」快取 Renderer，但素材庫（解碼好的貼圖）是一個檔案一份、
        # 所有選項共用。原本每種選項組合各建一份素材庫：換個背景色就把整份貼圖重新解碼，
        # 最多 32 份並存，每份最多 256 張圖——大檔切幾次選項就是好幾百 MB。
        self._renderers: OrderedDict[tuple, Renderer] = OrderedDict()
        self._renderer_limit = 8
        self._libraries: OrderedDict[str, AssetLibrary] = OrderedDict()
        self._library_limit = 6
        self._lock = threading.Lock()
        # 同時最多 3 個渲染（大檔一次就要幾百 MB），其中背景預抓最多佔 2 個：
        # 永遠留一個給使用者正在看的那一格，拖時間軸不必排在預抓後面。
        self._render_slots = threading.Semaphore(3)
        self._prefetch_lane = threading.Semaphore(2)
        # 節點樹、屬性面板會對同一格連續要好幾次 layout，算一次就好
        self._layouts: OrderedDict[tuple, RenderResult] = OrderedDict()
        self._layout_limit = 6
        # 檢查（doctor）一次只跑一份；結果依檔案指紋快取，檔案沒改就不重算
        self._checks: OrderedDict[tuple, dict] = OrderedDict()
        self._check_lock = threading.Lock()
        # 使用者正在看的那一格有幾張在算（不含預抓）；全專案檢查看到 > 0 就先讓
        self._interactive = 0
        self._scan: scan.ProjectScan | None = None
        self._files_cache: tuple[float, list[dict]] | None = None
        self._files_lock = threading.Lock()
        self._files_ttl = 2.0

    # ---- 檔案清單
    def list_files(self, fresh: bool = False) -> list[dict]:
        now = time.monotonic()
        with self._files_lock:
            if (not fresh and self._files_cache
                    and now - self._files_cache[0] < self._files_ttl):
                return self._files_cache[1]
        entries = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in SKIP_DIRS]
            for name in sorted(files):
                if name.lower().endswith(".csd"):
                    full = os.path.join(dirpath, name)
                    entries.append({
                        "path": os.path.relpath(full, self.root).replace("\\", "/"),
                        "name": os.path.splitext(name)[0],
                        "dir": os.path.relpath(dirpath, self.root).replace("\\", "/"),
                    })
        with self._files_lock:
            self._files_cache = (time.monotonic(), entries)
        return entries

    def resolve(self, rel: str) -> str:
        full = os.path.abspath(os.path.join(self.root, rel))
        try:
            inside = os.path.commonpath([self.root, full]) == self.root
        except ValueError:      # 不同磁碟機代號
            inside = False
        if not inside:
            raise ValueError("路徑超出根目錄範圍")
        if not os.path.isfile(full):
            raise FileNotFoundError(rel)
        return full

    # ---- 文件快取（檔案有改動就重讀）
    def document(self, full: str):
        stamp = _version(full)
        with self._lock:
            cached = self._docs.get(full)
            if cached and cached[0] == stamp:
                self._docs.move_to_end(full)
                return cached[1]
        doc = csd_parser.parse(full)
        with self._lock:
            cached = self._docs.get(full)
            if cached and cached[0] == stamp:
                # 另一條執行緒已經用同一個指紋解析完了，丟棄這份重複的
                self._docs.move_to_end(full)
                return cached[1]
            # 存的是「讀取前」的指紋，不是解析完之後才去問。
            # 解析途中檔案可能被換掉（MCP 或 agent 正在改），那時重新問到的是新指紋，
            # 拿它去標一份舊內容，之後就會一直拿這份舊的、再也不會重讀。
            # 用舊指紋標最多只是下一次多解析一遍，會自己修正回來。
            self._docs[full] = (stamp, doc)
            self._docs.move_to_end(full)
            while len(self._docs) > self._doc_limit:
                self._docs.popitem(last=False)
            return doc

    def render_slot(self):
        """限制同時進行的渲染數量。"""
        return self._render_slots

    @contextlib.contextmanager
    def interactive(self):
        """標記「使用者正在等的那一格」正在算，全專案檢查會先讓它。"""
        with self._lock:
            self._interactive += 1
        try:
            yield
        finally:
            with self._lock:
                self._interactive -= 1

    def interactive_busy(self) -> bool:
        with self._lock:
            return self._interactive > 0

    @property
    def scan(self) -> "scan.ProjectScan":
        with self._lock:
            if self._scan is None:
                self._scan = scan.ProjectScan(self)
            return self._scan

    def prefetch_lane(self):
        """背景預抓多一道限制，見 __init__。"""
        return self._prefetch_lane

    def library(self, full: str) -> AssetLibrary:
        """一個 .csd 一份素材庫，所有算圖選項共用。已經拿著 self._lock 時改用 _library_locked。"""
        with self._lock:
            return self._library_locked(full)

    def _library_locked(self, full: str) -> AssetLibrary:
        found = self._libraries.get(full)
        if found is not None:
            self._libraries.move_to_end(full)
            return found
        made = library_for(full, self.project_root)
        self._libraries[full] = made
        while len(self._libraries) > self._library_limit:
            self._libraries.popitem(last=False)
        return made

    def renderer(self, full: str, options: RenderOptions) -> Renderer:
        """依檔案與選項快取 Renderer，讓圖片／九宮格／文字光柵的快取能跨請求重用。"""
        key = (full, options.scale, options.background, options.annotate,
               options.annotate_filter, options.padding, options.inner_mode,
               options.draw_particles, options.full_bounds, options.max_pixels,
               options.layout_only)
        with self._lock:
            found = self._renderers.get(key)
            if found is not None:
                self._renderers.move_to_end(key)
                return found
            # 建在鎖裡：兩條請求同時未命中時不會做出兩份 Renderer。
            made = Renderer(self._library_locked(full), options)
            self._renderers[key] = made
            while len(self._renderers) > self._renderer_limit:
                self._renderers.popitem(last=False)
            return made

    def reset(self, full: str) -> None:
        """丟掉這個檔案的素材庫與 Renderer，下一次會重讀貼圖與字型（「重新整理」用）。"""
        with self._lock:
            self._libraries.pop(full, None)
            for key in [k for k in self._renderers if k[0] == full]:
                del self._renderers[key]
            for key in [k for k in self._layouts if k[0] == full]:
                del self._layouts[key]
            for key in [k for k in self._checks if k[0] == full]:
                del self._checks[key]

    def layout(self, full: str, options: RenderOptions, frame: float, animation: str,
               visibility: dict[str, bool]) -> tuple[object, RenderResult]:
        """只算版面（不畫像素）的結果，給節點樹與屬性面板共用。"""
        options.layout_only = True
        # 指紋先取、再解析：解析途中檔案被換掉的話，新內容會被標上舊指紋，
        # 下一次用新指紋查不到就重算——方向是安全的（同 document() 的理由）。
        key = (full, _version(full), frame, animation,
               tuple(sorted(visibility.items())), options.full_bounds,
               options.draw_particles, options.inner_mode)
        doc = self.document(full)
        with self._lock:
            found = self._layouts.get(key)
            if found is not None:
                self._layouts.move_to_end(key)
                return doc, found
        result = self.renderer(full, options).render(doc, frame, animation, visibility)
        with self._lock:
            self._layouts[key] = result
            while len(self._layouts) > self._layout_limit:
                self._layouts.popitem(last=False)
        return doc, result

    def check(self, full: str) -> dict:
        """跑 doctor 的全部檢查。結果跟 `csdview doctor` 完全一樣（同一個函式）。

        用一份新的素材庫：「缺少的資源」是素材庫在載入時記下來的，
        跟畫面共用的那份會混進使用者看過的其他影格，就跟 CLI 對不上了。
        """
        from .cli import diagnose

        key = (full, _version(full))
        with self._lock:
            found = self._checks.get(key)
            if found is not None:
                return found
        with self._check_lock:              # 一次一份，連續改檔也不會疊起來跑
            with self._lock:
                found = self._checks.get(key)
                if found is not None:
                    return found
            started = time.perf_counter()
            doc = self.document(full)
            renderer = Renderer(library_for(full, self.project_root),
                                RenderOptions(layout_only=True, full_bounds=True))
            payload, last = diagnose(doc, renderer)
            summary = details.check_summary(payload, [r for r, _p in numbered(last)])
            summary["ms"] = round((time.perf_counter() - started) * 1000)
            summary["mtime"], summary["size"] = key[1]
        with self._lock:
            self._checks[key] = summary
            while len(self._checks) > 16:
                self._checks.popitem(last=False)
        self.scan.record(full, summary, key[1])
        return summary


def numbered(result: RenderResult) -> list[tuple[NodeReport, int]]:
    """節點樹前序展開：(節點, 父節點的編號)，根的父節點是 -1。

    編號就是網頁上的節點 id。前序跟渲染時 reports 的順序一致，
    也就是後面的蓋在前面上——點畫面選節點時靠這個挑最上層的。
    """
    order: list[tuple[NodeReport, int]] = []
    stack = [(result.root, -1)] if result.root else []
    while stack:
        report, parent = stack.pop()
        index = len(order)
        order.append((report, parent))
        for child in reversed(report.children):
            stack.append((child, index))
    return order


class Handler(BaseHTTPRequestHandler):
    state: ViewerState = None            # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # 關掉逐筆請求輸出
        pass

    # ------------------------------------------------------------ 回應工具
    def _send(self, code: int, body: bytes, content_type: str,
              cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 影格圖片帶著 mtime 版本參數，內容不會變，所以標成可長期快取。
        # 實測（Chrome CDP）：同一次瀏覽期間的重複拖曳，不論有無此標頭都是 0 次
        # 網路請求——瀏覽器的記憶體圖片快取就擋掉了。真正的差別在重新載入頁面之後：
        # 有此標頭時 11 格全部命中快取，沒有時 11 格全部重新向伺服器要。
        self.send_header("Cache-Control",
                         "public, max-age=604800, immutable" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # ------------------------------------------------------------ 只接受這個檢視器自己的網頁
    def _from_this_page(self) -> bool:
        """擋掉別的網站打過來的請求。

        伺服器只聽 127.0.0.1，但瀏覽器裡的其他網站仍然打得到它。DNS rebinding（惡意網域改解析成
        127.0.0.1）會讓瀏覽器把對方當成同源、讀得到回應：.csd 清單、節點結構、渲染出來的畫面。
        這種請求的 Host 是對方的網域，所以只收 Host 是 127.0.0.1／localhost 加上本伺服器埠號的；
        帶 Origin 的（瀏覽器的跨站請求）也必須是自己。curl 之類不帶 Origin 的本機程式照常可用。
        """
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").strip().lower() not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.strip().lower() in {"http://" + h for h in allowed}

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:
        if not self._from_this_page():
            self.close_connection = True
            self._json({"error": "拒絕：請求不是來自這個檢視器的網頁（請用 http://127.0.0.1:埠號/ 開啟）"}, 403)
            return
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        try:
            if route in ("/", "/index.html"):
                with open(os.path.join(_HERE, "web", "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            elif route == "/api/files":
                fresh = query.get("fresh", "0") == "1"
                self._json({"root": self.state.root,
                            "files": self.state.list_files(fresh=fresh)})
            elif route == "/api/stat":
                self._json(self._stat(query))
            elif route == "/api/info":
                self._json(self._info(query))
            elif route == "/api/tree":
                self._json(self._tree(query))
            elif route == "/api/node":
                self._node(query)
            elif route == "/api/check":
                full = self.state.resolve(query.get("file", ""))
                self._json(self.state.check(full))
            elif route == "/api/scan":
                state = self.state.scan
                if query.get("start") == "1" or query.get("rescan") == "1":
                    state.start(force=query.get("rescan") == "1")
                if "pause" in query:
                    state.pause(query["pause"] == "1")
                try:
                    since = int(query.get("since", 0))
                except ValueError:
                    since = 0
                self._json(state.poll(since))
            elif route == "/api/texture":
                self._texture(query)
            elif route == "/api/reset":
                self.state.reset(self.state.resolve(query.get("file", "")))
                self._json({"ok": True})
            elif route == "/api/frame":
                self._frame(query)
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError as exc:
            self._json({"error": f"找不到檔案: {exc}"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 403)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ------------------------------------------------------------ API
    def _info(self, query) -> dict:
        full = self.state.resolve(query.get("file", ""))
        doc = self.state.document(full)
        library = self.state.library(full)
        return {
            "file": query.get("file", ""),
            "name": doc.name,
            "version": doc.version,
            "rootType": doc.root_type,
            "projectRoot": library.root,
            "size": [doc.root.width, doc.root.height],
            "duration": doc.duration,
            "fps": anim.FPS,
            "defaultAnimation": doc.active_animation,
            "animations": [
                {"name": c.name, "start": c.start, "end": c.end, "frames": c.length}
                for c in doc.clips
            ],
            "nodeCount": sum(1 for _ in doc.root.walk()),
            # 前端把這幾個一起放進圖片網址當版本參數，也用來判斷檔案有沒有被改
            **_stamp(full),
            "boot": self.state.boot,
        }

    def _stat(self, query) -> dict:
        """只回檔案的修改時間，給前端輪詢用。

        不解析 .csd，也不碰任何快取，所以每秒問一次也幾乎沒有成本。
        """
        full = self.state.resolve(query.get("file", ""))
        return {**_stamp(full), "boot": self.state.boot}

    def _options(self, query) -> RenderOptions:
        return RenderOptions(
            scale=float(query.get("scale", 1.0)),
            background=query.get("bg", "checker"),
            annotate=query.get("annotate", "0") == "1",
            annotate_filter=query.get("filter", ""),
            padding=int(query.get("pad", 0)),
            inner_mode=query.get("inner", "sync"),
            draw_particles=query.get("particles", "1") == "1",
            full_bounds=query.get("full", "0") == "1",
            layout_only=query.get("layout", "0") == "1",
        )

    @staticmethod
    def _visibility(query) -> dict[str, bool]:
        """hide/show 用 | 分隔節點路徑；節點名稱不會出現 | 所以夠安全。

        影格快取鍵本來就含整個查詢字串，所以切換可視狀態會自動取得新圖。
        """
        out: dict[str, bool] = {}
        for path in (query.get("hide") or "").split("|"):
            if path:
                out[path] = False
        for path in (query.get("show") or "").split("|"):
            if path:
                out[path] = True
        return out

    def _layout(self, query):
        full = self.state.resolve(query.get("file", ""))
        doc, result = self.state.layout(full, self._options(query),
                                        float(query.get("frame", 0)),
                                        query.get("anim", ""), self._visibility(query))
        return full, doc, result

    def _tree(self, query) -> dict:
        _full, _doc, result = self._layout(query)
        size = result.canvas_size or result.image.size

        # 每個節點帶一個 id（前序編號），屬性面板用它指名節點：
        # 名稱路徑可能重複（同一層兩個同名節點），編號不會。
        counter = [0]

        def to_dict(report: NodeReport) -> dict:
            data = report.to_dict(with_children=False)
            data["id"] = counter[0]
            counter[0] += 1
            if report.children:
                data["children"] = [to_dict(c) for c in report.children]
            return data

        return {
            "frame": float(query.get("frame", 0)),
            "imageSize": list(size),
            "tree": to_dict(result.root) if result.root else None,
            "missingAssets": result.missing,
            "events": result.events,
        }

    def _node(self, query) -> None:
        """屬性面板：一個節點在這一格的全部設定。"""
        full, doc, result = self._layout(query)
        order = numbered(result)
        path = query.get("path", "")
        try:
            index = int(query.get("id", -1))
        except ValueError:
            index = -1
        # id 是上一次節點樹的編號；檔案剛被改過的話可能已經對不上，改用路徑找
        if not (0 <= index < len(order)) or order[index][0].path != path:
            index = next((i for i, (r, _p) in enumerate(order) if r.path == path), -1)
        if index < 0:
            self._json({"error": "找不到這個節點（檔案可能剛被改過）"}, 404)
            return
        report = order[index][0]
        chain, parent = [], order[index][1]
        while parent >= 0:
            chain.append(order[parent][0])
            parent = order[parent][1]
        chain.reverse()

        name = query.get("anim", "")
        clip = doc.clip(name) if name else None
        span = (clip.start, clip.end) if clip else (0, doc.duration)
        body = details.node_details(report, chain, self.state.library(full),
                                    self._visibility(query), span, doc)
        self._json({"id": index, "path": report.path, "name": report.name,
                    "type": report.ctype.replace("ObjectData", ""), **body})

    def _texture(self, query) -> None:
        """屬性面板的貼圖預覽（九宮格切線畫在上面）。

        只給專案裡的圖：路徑解析後必須落在專案根目錄（或 --extra-root）底下，
        不然 `../../` 就能讀到磁碟上任何一張圖。大圖先縮到長邊 512，預覽用不到原尺寸，
        前端用百分比擺切線，縮多少都對得上。
        """
        full = self.state.resolve(query.get("file", ""))
        library = self.state.library(full)
        image = details.load_texture(library, query.get("type", "Normal"),
                                     query.get("path", ""), query.get("plist", ""))
        if image is None:
            self._json({"error": "找不到這張圖"}, 404)
            return
        image = image.copy()
        image.thumbnail((512, 512))
        buffer = io.BytesIO()
        image.save(buffer, "PNG", compress_level=3)
        self._send(200, buffer.getvalue(), "image/png")

    def _frame(self, query) -> None:
        rel = query.get("file", "")
        path = self.state.resolve(rel)
        # pf=1 是背景預抓。不進快取鍵：同一格預抓與直接要的是同一張圖。
        prefetch = query.pop("pf", "0") == "1"
        # 直接拿整組查詢參數當快取鍵：先前是逐一列舉，新增算圖參數時
        # 很容易忘了同步，結果讀到舊圖（新參數等於沒作用）
        key = (path, _version(path), tuple(sorted(query.items())))

        cached = self.state.cache.get(key)
        if cached is None:
            lane = self.state.prefetch_lane() if prefetch else contextlib.nullcontext()
            busy = contextlib.nullcontext() if prefetch else self.state.interactive()
            with busy, lane, self.state.render_slot():
                cached = self.state.cache.get(key)      # 排隊期間可能已被別人算好
                if cached is None:
                    doc = self.state.document(path)
                    renderer = self.state.renderer(path, self._options(query))
                    result = renderer.render(doc, float(query.get("frame", 0)),
                                             query.get("anim", ""),
                                             self._visibility(query))
                    buffer = io.BytesIO()
                    # 壓縮等級 3：大圖（4003x3146）編碼 430 -> 267 ms，檔案大約多兩成。
                    # 本機傳輸，省下的是 CPU。
                    result.image.save(buffer, "PNG", compress_level=3)
                    cached = buffer.getvalue()
                    self.state.cache.put(key, cached)
        self._send(200, cached, "image/png", cache=True)


_WANTED = [0]        # 使用者原本要的埠，只為了在換埠時講清楚


class _Server(ThreadingHTTPServer):
    """獨佔埠號的 HTTP server。

    預設的 SO_REUSEADDR 在 Windows 上允許兩個行程綁同一個埠：第二個**不會報錯**，
    但請求全被第一個接走。實測兩個 agent 各開一個專案、都用預設埠時，
    第二個看到「已啟動」，畫面卻是別人的專案——沒有任何警訊，最難查的那種。
    所以 Windows 改用 SO_EXCLUSIVEADDRUSE，讓第二次綁定乾脆失敗，
    由呼叫端換一個埠。POSIX 保留 SO_REUSEADDR（那裡不會被搶，只是避開 TIME_WAIT）。
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _bind(port: int, tries: int = 20):
    """從 port 開始往上找一個沒人用的埠。回傳 (server, 實際用的埠)。"""
    last = None
    for offset in range(tries):
        try:
            return _Server(("127.0.0.1", port + offset), Handler), port + offset
        except OSError as exc:
            last = exc
    raise SystemExit(f"錯誤: {port}~{port + tries - 1} 都被佔用了（{last}）。"
                     "用 --port 指定別的埠。")


def _lower_priority() -> bool:
    """把自己降到「低於標準」優先權。

    渲染是吃滿 CPU 的工作（大檔一格要兩三秒，背景還會預抓）。優先權照預設的話，
    預抓期間 Cocos Studio、瀏覽器、編輯器都會跟著頓；降一級之後別的程式要 CPU
    時一律先給它們，這裡只撿剩下的，自己看的那一格在電腦閒著時一樣快。
    """
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            # 型別一定要宣告：不宣告的話 ctypes 把 GetCurrentProcess 的 -1（偽 handle）
            # 當 32 位元整數傳，64 位元 Windows 上就變成無效 handle（實測錯誤碼 6），靜靜失敗。
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            below_normal = 0x00004000
            return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal))
        os.nice(5)
        return True
    except Exception:            # 拿不到權限就算了，照常服務
        return False


def serve(root: str = ".", port: int = 8770, open_browser: bool = True,
          project_root: str | None = None) -> None:
    Handler.state = ViewerState(root, project_root)
    _WANTED[0] = port
    server, port = _bind(port)
    url = f"http://127.0.0.1:{port}/"
    gentle = _lower_priority()

    print(f"csdview 網頁檢視器已啟動：{url}")
    if port != _WANTED[0]:
        print(f"（{_WANTED[0]} 已經有人在用了，改用 {port}）")
    print(f"瀏覽目錄：{Handler.state.root}")
    if gentle:
        print("渲染以較低優先權執行：其他程式要用 CPU 時會先讓給它們。")
    print("按 Ctrl+C 結束。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
