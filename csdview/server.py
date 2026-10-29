"""本機網頁檢視器。

渲染完全在 Python 端做，瀏覽器只負責顯示 PNG 與控制時間軸，
所以人在網頁上看到的畫面，跟 agent 用 CLI 取得的圖片是同一份程式碼算出來的。
"""
from __future__ import annotations

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

from . import anim, parser as csd_parser
from .assets import SKIP_DIRS, library_for
from .render import RenderOptions, Renderer

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
        self._renderers: dict[tuple, Renderer] = {}
        self._lock = threading.Lock()
        # 前端會同時預抓多格，大檔一次渲染就要幾百 MB，不設限會把記憶體吃爆
        self._render_slots = threading.Semaphore(3)
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

    def renderer(self, full: str, options: RenderOptions) -> Renderer:
        """依檔案與選項快取 Renderer，讓圖片／九宮格／文字光柵的快取能跨請求重用。"""
        key = (full, options.scale, options.background, options.annotate,
               options.annotate_filter, options.padding, options.inner_mode,
               options.draw_particles, options.full_bounds, options.max_pixels,
               options.layout_only)
        with self._lock:
            found = self._renderers.get(key)
            if found is not None:
                return found
            # 建在鎖裡：兩條請求同時未命中時不會做出兩份 AssetLibrary。
            if len(self._renderers) > 32:
                self._renderers.clear()
            made = Renderer(library_for(full, self.project_root), options)
            self._renderers[key] = made
            return made


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

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:
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
        library = library_for(full, self.state.project_root)
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

    def _tree(self, query) -> dict:
        full = self.state.resolve(query.get("file", ""))
        doc = self.state.document(full)
        options = self._options(query)
        options.layout_only = True
        renderer = self.state.renderer(full, options)
        frame = float(query.get("frame", 0))
        result = renderer.render(doc, frame, query.get("anim", ""),
                                 self._visibility(query))
        size = result.canvas_size or result.image.size
        return {
            "frame": frame,
            "imageSize": list(size),
            "tree": result.root.to_dict() if result.root else None,
            "missingAssets": result.missing,
            "events": result.events,
        }

    def _frame(self, query) -> None:
        rel = query.get("file", "")
        path = self.state.resolve(rel)
        # 直接拿整組查詢參數當快取鍵：先前是逐一列舉，新增算圖參數時
        # 很容易忘了同步，結果讀到舊圖（新參數等於沒作用）
        key = (path, _version(path), tuple(sorted(query.items())))

        cached = self.state.cache.get(key)
        if cached is None:
            with self.state.render_slot():
                cached = self.state.cache.get(key)      # 排隊期間可能已被別人算好
                if cached is None:
                    doc = self.state.document(path)
                    renderer = self.state.renderer(path, self._options(query))
                    result = renderer.render(doc, float(query.get("frame", 0)),
                                             query.get("anim", ""),
                                             self._visibility(query))
                    buffer = io.BytesIO()
                    result.image.save(buffer, "PNG")
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


def serve(root: str = ".", port: int = 8770, open_browser: bool = True,
          project_root: str | None = None) -> None:
    Handler.state = ViewerState(root, project_root)
    _WANTED[0] = port
    server, port = _bind(port)
    url = f"http://127.0.0.1:{port}/"

    print(f"csdview 網頁檢視器已啟動：{url}")
    if port != _WANTED[0]:
        print(f"（{_WANTED[0]} 已經有人在用了，改用 {port}）")
    print(f"瀏覽目錄：{Handler.state.root}")
    print("按 Ctrl+C 結束。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
