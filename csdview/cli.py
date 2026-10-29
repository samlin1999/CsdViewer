"""csdview 指令列介面 —— 這一層就是 AI agent 的操作介面。

設計原則：每個子指令都能加 --json，輸出穩定、可機器解析的結構；
圖片一律寫成檔案並在 JSON 裡回報路徑，agent 讀完 JSON 就知道要去看哪張圖。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import anim, parser as csd_parser
from .assets import SKIP_DIRS, atlas_map, library_for
from .locking import LockTimeout, ReplaceBusy, file_lock, replace_with_retry
from .model import Document
from .render import RenderOptions, Renderer, RenderResult


# ---------------------------------------------------------------- 共用

def _add_common(sub: argparse.ArgumentParser, with_render: bool = True,
                full_default: bool = True) -> None:
    sub.add_argument("csd", help=".csd 檔案路徑")
    sub.add_argument("--root", help="專案 cocosstudio 根目錄（預設自動偵測）")
    sub.add_argument("--extra-root", action="append", default=[],
                     help="額外的資源搜尋目錄，可重複指定")
    sub.add_argument("--json", action="store_true", help="以 JSON 輸出")
    sub.add_argument("--pretty", action="store_true",
                     help="JSON 加上縮排（給人看；預設是緊湊輸出，省 token）")
    if with_render:
        sub.add_argument("--anim", "-a", help="動畫名稱（省略則用整條時間軸）")
        sub.add_argument("--frame", "-f", type=float, default=0.0,
                         help="影格編號；配合 --anim 時是相對於該段的起點")
        sub.add_argument("--time", "-t", type=float,
                         help="以秒指定時間（60fps 換算），會覆蓋 --frame")
        sub.add_argument("--scale", "-s", type=float, default=1.0, help="輸出縮放倍率")
        sub.add_argument("--bg", default="checker",
                         help="背景：checker（預設）、none、或 #rrggbb")
        sub.add_argument("--annotate", action="store_true",
                         help="疊上節點外框與名稱，方便 agent 對應畫面與節點")
        sub.add_argument("--annotate-only", default="",
                         help="只標註名稱或路徑含此字串的節點")
        sub.add_argument("--pad", type=int, default=0, help="自動裁切時的外框留白")
        sub.add_argument("--max-depth", type=int, default=16, help="巢狀 .csd 展開層數上限")
        sub.add_argument("--inner-mode", choices=["sync", "first", "none"], default="sync",
                         help="巢狀 .csd 的處理：sync 跟隨父層時間、"
                              "first 固定在第一格、none 完全不展開")
        sub.add_argument("--font", help="指定文字用的字型（字型名稱、檔名或路徑），"
                                        "覆寫 .csd 裡的設定")
        sub.add_argument("--hide", action="append", default=[], metavar="節點路徑",
                         help="強制隱藏該節點（含子節點），可重複指定")
        sub.add_argument("--show", action="append", default=[], metavar="節點路徑",
                         help="強制顯示該節點，即使 .csd 裡設為隱藏；可重複指定")
        sub.add_argument("--no-particles", action="store_true", help="不畫粒子標示框")
        sub.add_argument("--full-bounds", action=argparse.BooleanOptionalAction,
                         default=full_default,
                         help="連設計畫布外的內容也畫出來，並以虛線標出原設計區"
                              f"（預設{'開啟' if full_default else '關閉'}）")
        sub.add_argument("--no-guide", dest="guide", action="store_false",
                         help="不要畫出設計區虛線框（做像素比對時應關閉）")
        sub.add_argument("--max-pixels", type=int, default=16_000_000,
                         help="畫布總像素上限，超過會自動降尺度（預設 1600 萬）")


def _visibility(args) -> dict[str, bool]:
    """把 --hide / --show 收成節點路徑到可視狀態的對照表。"""
    out: dict[str, bool] = {}
    for path in getattr(args, "hide", None) or []:
        out[path] = False
    for path in getattr(args, "show", None) or []:
        out[path] = True          # --show 後給，衝突時以顯示為準
    return out


def _build(args) -> tuple[Document, Renderer]:
    doc = csd_parser.parse(args.csd)
    library = library_for(args.csd, getattr(args, "root", None),
                          getattr(args, "extra_root", None),
                          getattr(args, "font", None))
    options = RenderOptions(
        scale=getattr(args, "scale", 1.0),
        background=getattr(args, "bg", "checker"),
        annotate=getattr(args, "annotate", False),
        annotate_filter=getattr(args, "annotate_only", ""),
        max_depth=getattr(args, "max_depth", 16),
        padding=getattr(args, "pad", 0),
        inner_mode=getattr(args, "inner_mode", "sync"),
        draw_particles=not getattr(args, "no_particles", False),
        full_bounds=getattr(args, "full_bounds", False),
        draw_guide=getattr(args, "guide", True),
        max_pixels=getattr(args, "max_pixels", 16_000_000),
    )
    return doc, Renderer(library, options)


def _frame_of(args, doc: Document) -> tuple[float, str]:
    """把 --anim / --frame / --time 換算成絕對影格。

    指定 --anim 時 --frame 是「該段動畫的第幾格」，所以要加上片段起點。
    """
    animation = getattr(args, "anim", None)
    start, end = anim.resolve_range(doc, animation)

    if getattr(args, "time", None) is not None:
        value = args.time * anim.FPS
    else:
        value = args.frame

    absolute = start + value if animation else value
    absolute = max(0.0, min(absolute, float(max(end, start))))
    return absolute, (animation or "")


def _frame_label(frame: float, animation: str, doc: Document) -> str:
    """把影格描述成人看得懂的字串，絕對與相對編號都寫出來。"""
    if not animation:
        return f"{frame:g} / {doc.duration}"
    start, end = anim.resolve_range(doc, animation)
    return (f"{frame:g}（動畫 '{animation}' 第 {frame - start:g} 格，"
            f"範圍 {start}~{end}）")


def _with_frame_fields(payload: dict, doc: Document, frame: float, animation: str) -> dict:
    """統一補上絕對影格與（指定動畫時的）相對影格，讓各指令的 JSON 欄位一致。"""
    payload["frame"] = frame
    payload["animation"] = animation
    if animation:
        start, _end = anim.resolve_range(doc, animation)
        payload["animationFrame"] = frame - start
    return payload


def _render_many(renderer, doc, frames, animation, visibility,
                 jobs: int, label: str, sink=None) -> list:
    """批次渲染多個影格，安全的時候用多執行緒。

    PIL 與 numpy 的重運算會釋放 GIL，實測 4 執行緒約 2.7 倍、8 執行緒約 3.3 倍。
    但每個並行渲染都要各自的畫布，大畫布時記憶體會翻倍，所以依畫布大小自動降低
    並行度；--jobs 可手動指定。
    """
    if not frames:
        return []

    first = renderer.render(doc, frames[0], animation, visibility)
    # 畫布大小要在 sink 之前取——sink 會把結果換成它的回傳值（例如檔案路徑）
    canvas_pixels = first.image.size[0] * first.image.size[1]
    if sink is not None:
        first = sink(0, frames[0], first)
    _progress(1, len(frames), label)
    if len(frames) == 1:
        return [first]

    if jobs <= 0:
        megapixels = canvas_pixels / 1e6
        if megapixels > 12:
            jobs = 1                      # 一張就好幾百 MB，不要並行
        elif megapixels > 4:
            jobs = 2
        else:
            jobs = min(4, os.cpu_count() or 1)

    rest = frames[1:]
    if jobs <= 1:
        out = [first]
        for index, value in enumerate(rest, 2):
            result = renderer.render(doc, value, animation, visibility)
            if sink is not None:
                result = sink(index - 1, value, result)
            out.append(result)
            _progress(index, len(frames), label)
        return out

    import concurrent.futures as futures
    import threading
    done = [1]
    lock = threading.Lock()

    def one(pair):
        offset, value = pair
        result = renderer.render(doc, value, animation, visibility)
        if sink is not None:
            # 存檔也放在工作執行緒裡：PNG 編碼會釋放 GIL，
            # 留在主執行緒序列存檔會把並行的效益吃掉一半
            result = sink(offset, value, result)
        with lock:
            done[0] += 1
            _progress(done[0], len(frames), label)
        return result

    with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        return [first] + list(pool.map(one, enumerate(rest, 1)))


def _progress(done: int, total: int, label: str) -> None:
    """把進度寫到 stderr。批次指令動輒跑上一分鐘，沒有輸出會被誤認為卡死；
    stdout 留給 --json，所以走 stderr。"""
    if total <= 1:
        return
    every = max(1, total // 10)
    if done == total or done % every == 0:
        pct = done * 100 // total
        print(f"  {label} {done}/{total} ({pct}%)", file=sys.stderr, flush=True)


def _slim(value):
    """把浮點數收乾淨：整數就不要帶 .0，其餘留 4 位。

    純粹是為了省 token。JSON 數字本來就不分整數浮點，讀的一方用 float() 取值不受影響。
    """
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return value
        rounded = round(value, 4)
        return int(rounded) if rounded == int(rounded) else rounded
    if isinstance(value, dict):
        return {k: _slim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_slim(v) for v in value]
    return value


def _json_text(payload, pretty: bool = False) -> str:
    """機器要讀的 JSON 一律緊湊輸出。

    縮排對解析毫無幫助，但很貴：實測 `tree`／`spec` 的輸出光是拿掉縮排就少了
    五到六成的 token（一個 130 節點的 .csd 從 22000 降到 8000 上下）。
    人要看的時候加 --pretty。
    """
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(_slim(payload), ensure_ascii=False, separators=(",", ":"))


def _emit(payload: dict, as_json: bool, lines: list[str],
          pretty: bool = False) -> None:
    if as_json:
        print(_json_text(payload, pretty))
    else:
        print("\n".join(lines))


# ---------------------------------------------------------------- info

def cmd_info(args) -> int:
    doc = csd_parser.parse(args.csd)
    library = library_for(args.csd, args.root, args.extra_root)

    counts: dict[str, int] = {}
    for node in doc.root.walk():
        key = node.ctype.replace("ObjectData", "")
        counts[key] = counts.get(key, 0) + 1

    references = sorted({
        n.file.path for n in doc.root.walk()
        if n.ctype == "ProjectNodeObjectData" and n.file and n.file.path
    })

    payload = {
        "file": doc.path,
        "name": doc.name,
        "version": doc.version,
        "rootType": doc.root_type,
        "projectRoot": library.root,
        "canvasSize": [doc.root.width, doc.root.height],
        "duration": doc.duration,
        "fps": anim.FPS,
        "speed": doc.speed,
        "defaultAnimation": doc.active_animation,
        "animations": [
            {"name": c.name, "start": c.start, "end": c.end,
             "frames": c.length, "seconds": round(c.length / anim.FPS, 3)}
            for c in doc.clips
        ],
        "timelines": len(doc.timelines),
        "animatedNodes": len(doc.timelines_by_tag()),
        "nodeCount": sum(counts.values()),
        "nodeTypes": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "references": references,
    }

    lines = [
        f"檔案     : {doc.path}",
        f"名稱     : {doc.name}  (版本 {doc.version}, 根節點 {doc.root_type})",
        f"專案根目錄: {library.root}",
        f"畫布尺寸 : {doc.root.width:g} x {doc.root.height:g}"
        + ("   ← 為 0 表示會依內容自動裁切" if not doc.root.width else ""),
        f"時間軸長 : {doc.duration} 影格 ({doc.duration / anim.FPS:.2f} 秒 @ {anim.FPS}fps)",
        f"節點總數 : {payload['nodeCount']}   有動畫的節點: {payload['animatedNodes']}",
        "",
        "動畫片段 :",
    ]
    if doc.clips:
        for clip in doc.clips:
            mark = "  *" if clip.name == doc.active_animation else "   "
            lines.append(f"{mark} {clip.name:<24} {clip.start:>5} ~ {clip.end:<5}"
                         f" ({clip.length} 影格, {clip.length / anim.FPS:.2f} 秒)")
    else:
        lines.append("    （無）")

    lines += ["", "節點類型 :"]
    for key, count in payload["nodeTypes"].items():
        lines.append(f"    {key:<20} {count}")

    if references:
        lines += ["", "引用的 .csd :"]
        lines += [f"    {r}" for r in references]

    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- render

def cmd_render(args) -> int:
    doc, renderer = _build(args)
    frame, animation = _frame_of(args, doc)
    result = renderer.render(doc, frame, animation, _visibility(args))

    out = args.out or _default_out(args.csd, animation, frame, args=args)
    _save(result, out)

    payload = _with_frame_fields(_render_payload(doc, result, out), doc, frame, animation)
    payload["clamped"] = result.clamped
    lines = [
        f"已輸出   : {out}",
        f"影格     : {_frame_label(frame, animation, doc)}",
        f"尺寸     : {result.image.size[0]} x {result.image.size[1]} px",
        f"可見節點 : {payload['visibleNodes']} / {payload['totalNodes']}",
    ]
    if result.design_rect and list(result.image.size) != [
            round(result.design_rect[2]), round(result.design_rect[3])]:
        d = result.design_rect
        lines.append(f"設計區   : ({d[0]:.0f},{d[1]:.0f}) {d[2]:.0f}x{d[3]:.0f}"
                     "（虛線框內；框外是畫布外的內容）")
    if result.clamped:
        lines.append(f"注意     : 畫布過大，已自動降到 {result.scale:.3f} 倍"
                     "（--max-pixels 可調整上限）")
    if result.events:
        lines.append(f"影格事件 : {', '.join(result.events)}")
    if result.missing:
        lines.append(f"缺少資源 : {len(result.missing)} 個（--json 可看清單）")
    _emit(payload, args.json, lines, args.pretty)
    return 0


def _render_payload(doc: Document, result: RenderResult, out: str) -> dict:
    return {
        "image": os.path.abspath(out),
        "file": doc.path,
        "frame": result.frame,
        "animation": result.animation,
        "imageSize": list(result.image.size),
        "scale": result.scale,
        "worldOrigin": [round(v, 2) for v in result.origin],
        "totalNodes": len(result.nodes),
        "visibleNodes": sum(1 for n in result.nodes if n.visible),
        "events": result.events,
        "missingAssets": result.missing,
        "designRect": ([round(v, 1) for v in result.design_rect]
                       if result.design_rect else None),
    }


# ---------------------------------------------------------------- sheet

def cmd_sheet(args) -> int:
    """把一段動畫平鋪成一張連續圖，agent 一眼看完整段動作。"""
    from PIL import Image, ImageDraw

    doc, renderer = _build(args)
    start, end = anim.resolve_range(doc, args.anim)
    count = max(1, args.count)
    if end <= start:
        frames = [float(start)]
    else:
        step = (end - start) / (count - 1) if count > 1 else 0
        frames = [start + step * i for i in range(count)]

    results = _render_many(renderer, doc, frames, args.anim or "",
                           _visibility(args), getattr(args, "jobs", 0), "渲染中")
    cell_w = max(r.image.size[0] for r in results)
    cell_h = max(r.image.size[1] for r in results)
    label_h = 18 if args.labels else 0

    cols = min(args.cols, len(results))
    rows = (len(results) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell_w, rows * (cell_h + label_h)), (24, 24, 28, 255))
    draw = ImageDraw.Draw(sheet)

    font = None
    if args.labels:
        try:
            from PIL import ImageFont
            from .assets import system_font_path
            path = system_font_path()
            font = ImageFont.truetype(path, 12) if path else None
        except Exception:
            font = None

    for index, result in enumerate(results):
        col, row = index % cols, index // cols
        x = col * cell_w + (cell_w - result.image.size[0]) // 2
        y = row * (cell_h + label_h) + label_h + (cell_h - result.image.size[1]) // 2
        sheet.alpha_composite(result.image, (x, y))
        if args.labels:
            text = f"#{int(round(result.frame))}  {result.frame / anim.FPS:.2f}s"
            draw.text((col * cell_w + 4, row * (cell_h + label_h) + 3), text,
                      font=font, fill=(180, 220, 255, 255))
        if index % cols == 0 and index:
            draw.line([0, row * (cell_h + label_h), sheet.size[0], row * (cell_h + label_h)],
                      fill=(60, 60, 68, 255))

    out = args.out or _default_out(args.csd, args.anim or "", None,
                                   suffix="_sheet", args=args)
    _save_image(sheet, out)

    payload = {
        "image": os.path.abspath(out),
        "file": doc.path,
        "animation": args.anim or "",
        "range": [start, end],
        "frames": [round(f, 2) for f in frames],
        "grid": [cols, rows],
        "cellSize": [cell_w, cell_h],
        "imageSize": list(sheet.size),
        "missingAssets": sorted(set(sum([r.missing for r in results], []))),
    }
    lines = [
        f"已輸出   : {out}",
        f"動畫     : {args.anim or '(整條時間軸)'}  影格 {start} ~ {end}",
        f"取樣     : {count} 格 -> {cols} x {rows} 格狀排列",
        f"尺寸     : {sheet.size[0]} x {sheet.size[1]} px",
    ]
    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- frames

def cmd_frames(args) -> int:
    """批次輸出每一格，給需要逐格比對的流程用。"""
    doc, renderer = _build(args)
    start, end = anim.resolve_range(doc, args.anim)
    step = max(1, args.step)

    if not args.out:
        args.out = os.path.join(_out_dir(), "frames_" + _option_tag(args))
    os.makedirs(args.out, exist_ok=True)
    todo = list(range(int(start), int(end) + 1, step))

    def save(offset, _value, result):
        path = os.path.join(args.out, f"{doc.name}_{todo[offset]:05d}.png")
        _save_image(result.image, path)
        return path

    paths = _render_many(renderer, doc, [float(f) for f in todo], args.anim or "",
                         _visibility(args), getattr(args, "jobs", 0), "輸出中", save)
    written = [{"frame": frame, "image": os.path.abspath(path)}
               for frame, path in zip(todo, paths)]

    payload = {
        "file": doc.path, "animation": args.anim or "",
        "range": [start, end], "step": step,
        "count": len(written), "outputDir": os.path.abspath(args.out), "images": written,
    }
    _emit(payload, args.json,
          [f"已輸出 {len(written)} 張到 {os.path.abspath(args.out)}"], args.pretty)
    return 0


# ---------------------------------------------------------------- gif

def cmd_gif(args) -> int:
    doc, renderer = _build(args)
    start, end = anim.resolve_range(doc, args.anim)
    step = max(1, args.step)
    frames = list(range(int(start), int(end) + 1, step)) or [int(start)]

    results = _render_many(renderer, doc, [float(f) for f in frames], args.anim or "",
                           _visibility(args), getattr(args, "jobs", 0), "渲染中")
    images = [r.image.convert("RGBA") for r in results]

    width = max(i.size[0] for i in images)
    height = max(i.size[1] for i in images)
    canvas_frames = []
    for image in images:
        base = _parse_bg(args.bg, width, height)
        base.alpha_composite(image, ((width - image.size[0]) // 2,
                                     (height - image.size[1]) // 2))
        canvas_frames.append(base.convert("P", palette=1, colors=255))

    out = args.out or _default_out(args.csd, args.anim or "", None,
                                   ext=".gif", args=args)
    duration_ms = int(round(1000 * step / anim.FPS))
    _save_image(canvas_frames[0], out, save_all=True,
                append_images=canvas_frames[1:], duration=duration_ms,
                loop=0, disposal=2)

    payload = {"image": os.path.abspath(out), "file": doc.path,
               "animation": args.anim or "", "frames": len(canvas_frames),
               "step": step, "durationMs": duration_ms * len(canvas_frames),
               "imageSize": [width, height]}
    _emit(payload, args.json,
          [f"已輸出   : {out}", f"影格數   : {len(canvas_frames)}（間隔 {step}）"], args.pretty)
    return 0


def _parse_bg(bg: str, width: int, height: int):
    from PIL import Image
    from .render import _parse_color
    if not bg or bg in ("none", "checker"):
        return Image.new("RGBA", (width, height), (24, 24, 28, 255))
    return Image.new("RGBA", (width, height), _parse_color(bg))


# ---------------------------------------------------------------- tree

def cmd_tree(args) -> int:
    doc, renderer = _build(args)
    frame, animation = _frame_of(args, doc)
    result = renderer.render(doc, frame, animation, _visibility(args))

    if args.json:
        payload = {
            "file": doc.path, "frame": frame, "animation": animation,
            "imageSize": list(result.image.size),
            "designRect": ([round(v, 1) for v in result.design_rect]
                           if result.design_rect else None),
            # --depth 對 JSON 一樣要生效。以前只有下面的文字輸出理它，
            # `tree --json --depth=2` 會把整棵樹倒出來，跟同一個指令的文字版對不起來。
            "tree": (result.root.to_dict(max_depth=args.depth)
                     if result.root else None),
        }
        print(_json_text(payload, args.pretty))
        return 0

    lines = [f"{doc.name}  @ 影格 {frame:g}"
             + (f"  動畫 '{animation}'" if animation else ""), ""]

    def walk(report, prefix: str, last: bool, depth: int) -> None:
        if depth > args.depth:
            return
        connector = "" if depth == 0 else ("└─ " if last else "├─ ")
        flags = []
        if not report.visible:
            flags.append("隱藏")
        if report.alpha < 0.999:
            flags.append(f"a={report.alpha:.2f}")
        if report.document:
            flags.append(f"→{report.document}")
        note = ("  [" + ", ".join(flags) + "]") if flags else ""
        kind = report.ctype.replace("ObjectData", "")
        rect = report.rect
        lines.append(f"{prefix}{connector}{report.name or '(無名)'}  <{kind}>"
                     f"  {rect[2]:.0f}x{rect[3]:.0f} @({rect[0]:.0f},{rect[1]:.0f}){note}")
        child_prefix = prefix + ("" if depth == 0 else ("   " if last else "│  "))
        for index, child in enumerate(report.children):
            walk(child, child_prefix, index == len(report.children) - 1, depth + 1)

    if result.root:
        walk(result.root, "", True, 0)
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------- find

def cmd_find(args) -> int:
    doc, renderer = _build(args)
    frame, animation = _frame_of(args, doc)
    result = renderer.render(doc, frame, animation, _visibility(args))

    needle = args.name.lower()
    kind = (args.type or "").lower()

    def keep(node) -> bool:
        """--name 與 --type 同時給就要都符合。

        先前寫成 or 相連，而空的 --name 會讓 `needle in name` 恆為真，
        結果單獨用 --type 時完全過濾不掉任何節點。
        """
        if needle and needle not in node.name.lower() and needle not in node.path.lower():
            return False
        if kind and kind not in node.ctype.lower():
            return False
        return True

    matches = [n for n in result.nodes if keep(n)]

    payload = {
        "file": doc.path, "frame": frame, "animation": animation,
        "query": args.name, "matches": [n.to_dict(with_children=False) for n in matches],
        "count": len(matches),
        "imageSize": list(result.image.size),
        "designRect": ([round(v, 1) for v in result.design_rect]
                       if result.design_rect else None),
    }
    lines = [f"符合 {len(matches)} 個節點  @ 影格 {frame:g}", ""]
    for node in matches:
        rect = node.rect
        state = "" if node.visible else "  [隱藏]"
        lines.append(f"  {node.path}  <{node.ctype.replace('ObjectData', '')}>"
                     f"  {rect[2]:.0f}x{rect[3]:.0f} @({rect[0]:.0f},{rect[1]:.0f}){state}")
        if node.source:
            lines.append(f"      資源: {node.source}")
        if node.text:
            lines.append(f"      文字: {node.text[:60]}")
    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- compare

def cmd_compare(args) -> int:
    """把渲染結果跟一張參考圖（例如 Cocos Creator 截圖）對照。"""
    from PIL import Image

    from .compare import compare as run_compare

    # 兩邊都攤在同一個底色上，透明區域才不會製造假差異
    args.bg = args.diff_bg
    # 虛線設計框是給人看的標註，會被當成像素差異，比對時一律不畫
    args.guide = False
    doc, renderer = _build(args)
    frame, animation = _frame_of(args, doc)
    result = renderer.render(doc, frame, animation, _visibility(args))

    reference = Image.open(args.reference)
    outcome = run_compare(
        result, reference, threshold=args.threshold, background=args.diff_bg,
        leaf_only=not args.all_nodes, top=args.top,
        make_image=not args.no_diff_image)

    out = None
    if outcome.image is not None:
        out = args.out or _default_out(args.csd, animation, frame,
                                       suffix="_diff", args=args)
        _save_image(outcome.image, out)

    payload = outcome.to_dict()
    payload.update({
        "file": doc.path, "reference": os.path.abspath(args.reference),
        "diffImage": os.path.abspath(out) if out else None,
        "missingAssets": result.missing,
    })
    _with_frame_fields(payload, doc, frame, animation)

    verdict_text = {"match": "一致", "shifted": "內容一致，整體位移",
                    "close": "接近", "different": "有明顯差異"}
    lines = [
        f"判定     : {verdict_text[outcome.verdict]}  ({outcome.verdict})",
        f"渲染尺寸 : {outcome.size[0]} x {outcome.size[1]}"
        + (f"   參考圖 {outcome.reference_size[0]} x {outcome.reference_size[1]}"
           " (已縮放對齊)" if outcome.resized else ""),
        f"平均差異 : {outcome.mae:.4f}   RMSE {outcome.rmse:.4f}   PSNR {outcome.psnr:.1f} dB",
        f"差異像素 : {outcome.diff_ratio * 100:.2f}%   形狀 IoU {outcome.alpha_iou:.3f}",
    ]
    if outcome.aspect_delta > 0.02:
        lines.append(f"注意     : 兩張圖長寬比差 {outcome.aspect_delta * 100:.1f}%，"
                     "縮放對齊會扭曲畫面、差異數字會偏高")
    if outcome.shifted:
        lines.append(
            f"整體位移 : 參考圖平移 ({outcome.shift[0]}, {outcome.shift[1]}) px 可對齊"
            f"（信心 {outcome.shift_confidence:.0f}）")
        lines.append(
            f"扣除位移 : 差異像素 {outcome.aligned_diff_ratio * 100:.2f}%"
            f"   平均差異 {outcome.aligned_mae:.4f}")

    regions = outcome.aligned_regions if outcome.shifted else outcome.regions
    if regions:
        title = "扣除位移後差異最大的節點：" if outcome.shifted else "差異最大的節點："
        lines += ["", title]
        for region in regions:
            lines.append(
                f"  {region.coverage * 100:5.1f}%  {region.path}"
                f"  <{region.ctype.replace('ObjectData', '')}>"
                f"  @({region.rect[0]:.0f},{region.rect[1]:.0f})"
                f" {region.rect[2]:.0f}x{region.rect[3]:.0f}")
    if outcome.hints:
        lines += ["", "判讀建議 :"]
        for hint in outcome.hints:
            lines.append("  - " + hint.replace("**", ""))
    if out:
        lines += ["", f"對照圖   : {out}"]
    _emit(payload, args.json, lines, args.pretty)
    return 0 if outcome.verdict != "different" else 1


# ---------------------------------------------------------------- spec

def cmd_spec(args) -> int:
    """匯出節點規格表，供移植後做結構化比對。"""
    doc, renderer = _build(args)
    frame, animation = _frame_of(args, doc)
    result = renderer.render(doc, frame, animation, _visibility(args))

    # --node / --depth 只縮小輸出範圍，不影響渲染：整份文件照樣算完，
    # 座標系（worldOrigin、designRect、imageSize）才會跟不篩選時完全一致。
    # 先篩再算的話子樹會自己變成一個新畫布，回報的座標就對不上原圖了。
    picked = result.nodes
    base_depth = 0
    if args.node:
        wanted = args.node.strip().strip("/")
        subtree = [n for n in result.nodes
                   if n.path == wanted or n.path.startswith(wanted + "/")]
        if not subtree:
            print(f"錯誤: 找不到節點 '{args.node}'。", file=sys.stderr)
            near = [n.path for n in result.nodes
                    if wanted.lower() in n.path.lower()][:8]
            if near:
                print("      名稱接近的有：" + "、".join(near), file=sys.stderr)
            else:
                print("      用 `csdview tree` 或 `csdview find` 取得正確的節點路徑。",
                      file=sys.stderr)
            return 2
        picked = subtree
        base_depth = min(n.depth for n in subtree)

    if args.depth < 99:
        picked = [n for n in picked if n.depth - base_depth <= args.depth]

    nodes = []
    for node in picked:
        entry = node.to_dict(with_children=False)
        quad = [[round(px, 2), round(py, 2)] for px, py in node.quad]
        # 沒有旋轉／skew 的節點，quad 就是 screenRect 的四個角，寫出來只是重複。
        # 一個節點 8 個數字，整份 spec 省下來的量很可觀。
        x, y, w, h = entry["screenRect"]
        upright = [[round(x, 2), round(y + h, 2)], [round(x + w, 2), round(y + h, 2)],
                   [round(x + w, 2), round(y, 2)], [round(x, 2), round(y, 2)]]
        if any(abs(a - b) > 0.01 for pa, pb in zip(quad, upright) for a, b in zip(pa, pb)):
            entry["quad"] = quad
        nodes.append(entry)

    payload = {
        "file": doc.path,
        "name": doc.name,
        "canvasSize": [doc.root.width, doc.root.height],
        "imageSize": list(result.image.size),
        "scale": result.scale,
        "worldOrigin": [round(v, 2) for v in result.origin],
        "designRect": ([round(v, 1) for v in result.design_rect]
                       if result.design_rect else None),
        "coordinateNote": "screenRect 為畫布像素座標，原點左上、Y 軸向下；"
                          "designRect 是設計解析度區域在圖上的位置",
        "nodeCount": len(nodes),
        "nodes": nodes,
        "missingAssets": result.missing,
    }
    # 篩選過就要說出來，並且附上整份的節點數。少了這兩個欄位，呼叫端分不出
    # 「這個檔只有這幾個節點」和「這次只要了一部分」——拿子集去做結構比對
    # 會得出「對方多了一堆節點」的錯結論。
    if len(nodes) != len(result.nodes):
        payload["totalNodeCount"] = len(result.nodes)
        payload["filter"] = {k: v for k, v in
                             (("node", args.node), ("depth", args.depth))
                             if v and v != 99}
    _with_frame_fields(payload, doc, frame, animation)

    if args.out:
        def write(path: str) -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_json_text(payload, args.pretty))

        _atomic_save(write, args.out)
        # 同時給了 --json 時，stdout 也必須是合法 JSON，否則呼叫端解析會失敗
        summary = {k: payload[k] for k in
                   ("file", "name", "frame", "animation", "imageSize",
                    "designRect", "nodeCount") if k in payload}
        summary["output"] = os.path.abspath(args.out)
        _emit(summary, args.json,
              [f"已輸出   : {args.out}   （{len(nodes)} 個節點）"], args.pretty)
        return 0

    print(_json_text(payload, args.pretty))
    return 0


# ---------------------------------------------------------------- doctor

def _stale_layout(doc: Document, tolerance: float = 1.5) -> list[dict]:
    """找出百分比版面與存下來的絕對值對不上的節點。

    Cocos Studio 會把 PreSize/PrePosition（百分比）與 Size/Position（絕對值）
    同時存檔，正常情況兩者一致。對不上就代表父層尺寸改過、而這個節點沒有重算，
    多半是沒跟著更新的舊資源——移植到引擎後位置會跟編輯器看到的不一樣。
    """
    out: list[dict] = []

    def walk(node, parent_w: float, parent_h: float, path: str):
        for child in node.children:
            here = f"{path}/{child.name}" if path else child.name
            issues = []
            if child.pre_size and parent_w > 0 and child.percent_size[0]:
                want = child.pre_size[0] * parent_w
                if abs(want - child.width) > tolerance:
                    issues.append({"field": "width", "stored": round(child.width, 1),
                                   "fromPercent": round(want, 1)})
            if child.pre_size and parent_h > 0 and child.percent_size[1]:
                want = child.pre_size[1] * parent_h
                if abs(want - child.height) > tolerance:
                    issues.append({"field": "height", "stored": round(child.height, 1),
                                   "fromPercent": round(want, 1)})
            if child.pre_position and parent_w > 0 and child.percent_position[0]:
                want = child.pre_position[0] * parent_w
                if abs(want - child.x) > tolerance:
                    issues.append({"field": "x", "stored": round(child.x, 1),
                                   "fromPercent": round(want, 1)})
            if child.pre_position and parent_h > 0 and child.percent_position[1]:
                want = child.pre_position[1] * parent_h
                if abs(want - child.y) > tolerance:
                    issues.append({"field": "y", "stored": round(child.y, 1),
                                   "fromPercent": round(want, 1)})
            if issues:
                out.append({"path": here, "parentSize": [round(parent_w, 1),
                                                         round(parent_h, 1)],
                            "mismatches": issues})
            walk(child, child.width or parent_w, child.height or parent_h, here)

    walk(doc.root, doc.root.width, doc.root.height, doc.root.name)
    return out


# 這些型別靠 Size 決定畫多大，尺寸 0 就等於沒有東西可畫。
# Sprite 不在裡面：它一律用貼圖原始尺寸，Size 0 是正常的。
_SIZED_WIDGETS = frozenset({
    "ButtonObjectData", "CheckBoxObjectData", "ImageViewObjectData",
    "LoadingBarObjectData", "SliderObjectData",
})

_IMAGE_FIELDS = ("FileData", "NormalFileData", "NormalBackFileData",
                 "ImageFileData", "BackGroundData", "NodeNormalFileData")


def _zero_size_widgets(csd_path: str) -> list[dict]:
    """找出「有貼圖但尺寸是 0」的元件。

    csdview 畫 Button 時用貼圖原始尺寸，所以尺寸 0 這裡看起來完全正常；
    Cocos Studio 是照 Size 排版，0x0 的元件在編輯器裡直接不顯示。
    掃過 1627 個實際檔案，1878 個 Button 沒有任何一個是 0x0 ——
    這種寫法在正常檔案裡不存在，出現就是建檔時漏了設尺寸。
    """
    try:
        tree = ET.fromstring(csd_parser._read_bytes(csd_path))
    except (ET.ParseError, OSError):
        return []

    parents = {child: parent for parent in tree.iter() for child in parent}

    def node_path(el) -> str:
        names, cur = [], el
        while cur is not None:
            name = cur.get("Name")
            if name and ("ObjectData" in cur.tag or cur.get("ctype")):
                names.append(name)
            cur = parents.get(cur)
        return "/".join(reversed(names))

    out = []
    for el in tree.iter():
        ctype = el.get("ctype")
        if ctype not in _SIZED_WIDGETS:
            continue
        size = el.find("Size")
        w = float(size.get("X", 0) or 0) if size is not None else 0.0
        h = float(size.get("Y", 0) or 0) if size is not None else 0.0
        # 只認「兩邊都是 0」。單邊 0 是進度條填充的正常寫法——寬度由程式在
        # 執行時撐開，實際專案裡就有 11 個（0x36、56x0 這種），報出來是誤報。
        if w > 0 or h > 0:
            continue
        image = next((el.find(f).get("Path") for f in _IMAGE_FIELDS
                      if el.find(f) is not None and el.find(f).get("Path")), "")
        if not image:
            continue                      # 沒有貼圖的空容器，尺寸 0 不算錯
        out.append({"path": node_path(el), "type": ctype.replace("ObjectData", ""),
                    "size": [w, h], "image": image})
    return out

def _atlas_mismatch(csd_path: str, root: str) -> list[dict]:
    """找出「圖被打包進圖集，.csd 卻沒指向 plist」的引用。

    這是 csdview 唯一驗得出「Cocos Studio 會掉圖、但這裡渲染正常」的檢查：
    csdview 在磁碟上找得到散圖就畫得出來，編輯器卻是照資源樹解析，
    被打包的圖寫成 Type="Normal" 就找不到（見 assets.atlas_map）。

    刻意讀原始 XML 而不是走 model：button 的 PressedFileData／DisabledFileData
    渲染用不到、model 也沒收，但它們一樣會在編輯器裡掉圖。
    """
    table = atlas_map(root)
    if not table:
        return []
    try:
        tree = ET.fromstring(csd_parser._read_bytes(csd_path))
    except (ET.ParseError, OSError):
        return []

    parents = {child: parent for parent in tree.iter() for child in parent}

    def node_path(el) -> str:
        names = []
        cur = parents.get(el)
        while cur is not None:
            name = cur.get("Name")
            # 根節點的 tag 是 ObjectData，子節點是 AbstractNodeData 靠 ctype 分辨
            if name and ("ObjectData" in cur.tag or cur.get("ctype")):
                names.append(name)
            cur = parents.get(cur)
        return "/".join(reversed(names))

    seen, out = set(), []
    for el in tree.iter():
        ref = (el.get("Path") or "").replace("\\", "/")
        if not ref:
            continue
        want = table.get(ref.lower())
        if not want:
            continue                      # 這張圖沒被打包，Normal 是對的
        have = (el.get("Plist") or "").replace("\\", "/")
        if have and have.lower() == want.lower():
            continue
        key = (node_path(el), el.tag, ref)
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": key[0], "element": el.tag, "image": ref,
                    "type": el.get("Type") or "", "plist": have,
                    "expectedPlist": want})
    return out

def cmd_doctor(args) -> int:
    doc, renderer = _build(args)
    # doctor 回報的每一項都不需要那張圖：缺少的資源是在解析素材時記下來的、
    # textOverflow 來自文字量測、staleLayout/atlasMismatch/zeroSize 只讀 .csd。
    # 所以關掉像素合成——下面要探測多個影格，畫出來的圖全部會被丟掉。
    # 實測 540 KB 的檔 1826 ms -> 137 ms；250 個檔的輸出逐欄位完全相同。
    renderer.options.layout_only = True
    start, end = anim.resolve_range(doc, None)
    probes = sorted({0, int(start), int((start + end) / 2), int(end)})
    last = None
    for frame in probes:
        last = renderer.render(doc, float(frame), "", _visibility(args))

    missing = sorted(renderer.assets.missing)
    stale = _stale_layout(doc)
    overflow = [
        {"path": n.path, "box": [round(v, 1) for v in n.size],
         "needs": [round(v, 1) for v in n.overflow]}
        for n in last.nodes if n.overflow
    ]
    atlas = _atlas_mismatch(doc.path, renderer.assets.root)
    zero = _zero_size_widgets(doc.path)
    payload = {
        "file": doc.path, "projectRoot": renderer.assets.root,
        "framesProbed": probes, "missingAssets": missing,
        "staleLayout": stale, "textOverflow": overflow,
        "atlasMismatch": atlas, "zeroSize": zero,
        "ok": not missing and not stale and not overflow and not atlas and not zero,
    }
    lines = [f"專案根目錄: {renderer.assets.root}",
             f"檢查影格  : {probes}"]
    if missing:
        lines += [f"缺少 {len(missing)} 個資源:"] + [f"    {m}" for m in missing]
    else:
        lines.append("所有資源都找得到。")
    if overflow:
        lines += ["", f"文字超出固定框的節點 {len(overflow)} 個"
                      "（cocos 不會裁切，但引擎端同樣會爆版）:"]
        for item in overflow:
            lines.append(f"    {item['path']}  框 {item['box'][0]:g}x{item['box'][1]:g}"
                         f"  實際需要 {item['needs'][0]:g}x{item['needs'][1]:g}")
    if zero:
        lines += ["", f"有貼圖但尺寸是 0 的元件 {len(zero)} 個"
                      "（這裡照貼圖原始尺寸畫，但 Cocos Studio 不會顯示）:"]
        for item in zero:
            lines.append(f"    {item['path']}  {item['type']}  {item['image']}")
    if atlas:
        lines += ["", f"貼圖沒有指向圖集的節點 {len(atlas)} 個"
                      "（這裡渲染正常，但 Cocos Studio 開起來會掉圖）:"]
        for item in atlas:
            lines.append(f"    {item['path']}  {item['element']}  {item['image']}")
            lines.append(f"        Type={item['type'] or '(空)'} Plist="
                         f"{item['plist'] or '(空)'}  ->  應指向 {item['expectedPlist']}")
    if stale:
        lines += ["", f"版面過期的節點 {len(stale)} 個"
                      "（百分比與絕對值對不上，父層改過但沒重算）:"]
        for item in stale:
            detail = "、".join(
                f"{m['field']} 存 {m['stored']:g} 但百分比算出 {m['fromPercent']:g}"
                for m in item["mismatches"])
            lines.append(f"    {item['path']}  {detail}")
    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- list

def cmd_list(args) -> int:
    entries = []
    for dirpath, _dirs, files in os.walk(args.directory):
        for name in sorted(files):
            if name.lower().endswith(".csd"):
                full = os.path.join(dirpath, name)
                entry = {"file": os.path.abspath(full),
                         "relative": os.path.relpath(full, args.directory)}
                if args.detail:
                    try:
                        doc = csd_parser.parse(full)
                        entry.update({
                            "name": doc.name, "duration": doc.duration,
                            "size": [doc.root.width, doc.root.height],
                            "animations": [c.name for c in doc.clips],
                        })
                    except Exception as exc:
                        entry["error"] = str(exc)
                entries.append(entry)

    payload = {"directory": os.path.abspath(args.directory),
               "count": len(entries), "files": entries}
    lines = [f"找到 {len(entries)} 個 .csd："]
    for entry in entries:
        extra = ""
        if args.detail and "duration" in entry:
            names = ", ".join(entry["animations"]) or "-"
            extra = f"   [{entry['duration']} 影格; {names}]"
        lines.append(f"  {entry['relative']}{extra}")
    _emit(payload, args.json, lines, args.pretty)
    return 0


# ---------------------------------------------------------------- init

# 用標記把自動產生的內容框起來，重跑 init 只會換掉這一段，
# 使用者自己寫在同一個檔案裡的內容不會被蓋掉。
_MARK_BEGIN = "<!-- csdview:begin 由 csdview init 產生，重跑會覆蓋這一段 -->"
_MARK_END = "<!-- csdview:end -->"

# 記住裝過哪些專案。說明檔之後會持續更新，有這份清單才能一次補齊全部，
# 否則裝了五個專案就得手動再跑五次。
# 兩邊必須算出同一個路徑，否則登錄檔會分家。
# CSDVIEW_PROJECTS 是給測試與 CI 用的：沒有它就只能動使用者真正的那一份，
# 每跑一次測試都會污染它（實測發生過兩次）。
_REGISTRY = (os.environ.get("CSDVIEW_PROJECTS")
             or os.path.join(os.path.expanduser("~"), ".csdview_projects"))


def _looks_like_source(directory: str) -> bool:
    """判斷這個資料夾像不像 .csd 的來源。"""
    if os.path.basename(directory).lower() == "cocosstudio":
        return True
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    return any(n.lower().endswith(".ccs") for n in names)


def _detect_source(start: str, depth: int = 3) -> list[str]:
    """從指定位置往下找 .csd 的來源資料夾。

    只找有限層數：專案動輒上萬個檔案，全掃會讓使用者等很久。
    """
    found: list[str] = []
    start = os.path.abspath(start)
    stack = [(start, 0)]
    while stack and len(found) < 5:
        current, level = stack.pop(0)
        if _looks_like_source(current):
            # 有 cocosstudio 子資料夾就用它——FileData.Path 是以它為基準
            nested = os.path.join(current, "cocosstudio")
            found.append(nested if os.path.isdir(nested) else current)
            continue                      # 找到就不再往裡面挖
        if level >= depth:
            continue
        try:
            entries = sorted(os.listdir(current))
        except OSError:
            continue
        for name in entries:
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            child = os.path.join(current, name)
            if os.path.isdir(child):
                stack.append((child, level + 1))
    return found


def _count_csd(directory: str, limit: int = 20000) -> int:
    total = 0
    for _dir, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        total += sum(1 for f in files if f.lower().endswith(".csd"))
        if total > limit:
            break
    return total


def _source_section(source: str | None) -> str:
    """產生指名來源資料夾的段落。

    工具本身的路徑刻意不寫死（靠 pip install 註冊指令），但「這個專案的 .csd 放在哪」
    是每個專案各自不同的事實，不寫下來 agent 就只能猜或反問。
    """
    if not source:
        return ""
    count = _count_csd(source)
    return (
        "## 這個專案的 .csd 在哪裡\n\n"
        f"```\n{source}\n```\n\n"
        f"（設定當下掃到 {count} 個 `.csd`）\n\n"
        "所有 csdview 指令都用這個資料夾底下的檔案。先跑這個確認能用：\n\n"
        "```bash\n"
        f'csdview list "{source}" --detail --json\n'
        "```\n\n"
        "若資料夾搬過位置，在 csdview 資料夾重跑一次 `設定AI助手.bat` 更新即可。\n\n"
        "---\n\n"
    )


def _agent_doc() -> str | None:
    """找出要安裝到目標專案的 agent 說明檔。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(os.path.dirname(here), "dropin", "CLAUDE.md")
    return candidate if os.path.isfile(candidate) else None


def _load_registry() -> list[dict]:
    try:
        with open(_REGISTRY, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("directory")]


def _write_registry(entries: list[dict]) -> None:
    """原子寫入登錄檔。呼叫端必須已經持有 `file_lock(_REGISTRY)`。

    鎖檔路徑是 `~/.csdview_projects.lock`，跟 mcpServer 的 add_project 約定好的，
    兩邊同時改才不會互相蓋掉。
    """
    folder = os.path.dirname(_REGISTRY) or "."
    handle, temp = tempfile.mkstemp(dir=folder, suffix=".json.tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        replace_with_retry(temp, _REGISTRY)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def _save_registry(entries: list[dict]) -> None:
    try:
        with file_lock(_REGISTRY):
            _write_registry(entries)
    except (OSError, LockTimeout, ReplaceBusy):
        pass          # 記不住就下次再指定一次，不值得中斷


def _remember(directory: str, name: str, csd_root: str | None = None) -> None:
    try:
        with file_lock(_REGISTRY):
            entries = [e for e in _load_registry()
                       if not (os.path.normcase(e["directory"]) == os.path.normcase(directory)
                               and e.get("name", "CLAUDE.md") == name)]
            record = {"directory": directory, "name": name}
            if csd_root:
                record["csdRoot"] = csd_root
            entries.append(record)
            _write_registry(entries)
    except (OSError, LockTimeout):
        pass


def _install_doc(source: str, directory: str, name: str,
                 csd_root: str | None = None) -> tuple[str, str]:
    """把說明檔寫進一個專案，回傳 (動作, 目標路徑)。"""
    with open(source, encoding="utf-8") as fh:
        body = fh.read().strip()
    if csd_root:
        # 來源段落擺在最前面，agent 一讀就知道要去哪裡找 .csd
        gap = chr(10) + chr(10)
        head, _, rest = body.partition(gap)
        body = head + gap + _source_section(csd_root) + rest
    block = _MARK_BEGIN + "\n\n" + body + "\n\n" + _MARK_END + "\n"

    target = os.path.join(directory, name)
    action = "建立"
    merged = block
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            existing = fh.read()
        if _MARK_BEGIN in existing and _MARK_END in existing:
            head = existing.split(_MARK_BEGIN)[0]
            tail = existing.split(_MARK_END, 1)[1]
            merged = head + block + tail.lstrip("\n")
            action = "更新"
        else:
            # 保留使用者原本寫的內容，說明附加在後面
            merged = existing.rstrip() + "\n\n" + block
            action = "附加"

    def write(path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(merged)

    _atomic_save(write, target)
    _remember(directory, name, csd_root)
    return action, target


def cmd_init(args) -> int:
    """把 agent 說明檔裝進一個或多個專案。

    做完這一步，那些專案裡的 AI agent 每次開始工作都會讀到 csdview 的用法，
    不必再逐次交代。
    """
    if args.list:
        entries = _load_registry()
        payload = {"projects": entries, "count": len(entries)}
        lines = [f"已設定過的專案 {len(entries)} 個："] if entries else ["還沒有設定過任何專案。"]
        for entry in entries:
            missing = "" if os.path.isdir(entry["directory"]) else "   （資料夾已不存在）"
            lines.append(f"  {entry['directory']}  ->  {entry.get('name', 'CLAUDE.md')}{missing}")
        _emit(payload, args.json, lines, args.pretty)
        return 0

    source = _agent_doc()
    if source is None:
        print("錯誤: 找不到 dropin/CLAUDE.md。", file=sys.stderr)
        print("      請在 csdview 專案資料夾內執行，或改用 pip install -e . 安裝。",
              file=sys.stderr)
        return 2

    explicit = getattr(args, "source", None)
    explicit = os.path.abspath(explicit) if explicit else None
    targets: list[tuple[str, str, str | None]] = [
        (os.path.abspath(d), args.name, explicit) for d in (args.directory or [])
    ]
    if args.all:
        seen = {(d, n) for d, n, _s in targets}
        for entry in _load_registry():
            key = (os.path.abspath(entry["directory"]), entry.get("name", "CLAUDE.md"))
            if key not in seen:
                targets.append((key[0], key[1], entry.get("csdRoot")))

    if not targets:
        print("錯誤: 請指定至少一個專案資料夾，或用 --all 更新所有設定過的專案。",
              file=sys.stderr)
        return 2

    done, skipped = [], []
    for directory, name, csd_root in targets:
        if not os.path.isdir(directory):
            skipped.append(directory)
            continue
        if csd_root and not os.path.isdir(csd_root):
            csd_root = None
        if csd_root is None:
            guesses = _detect_source(directory)
            csd_root = guesses[0] if len(guesses) == 1 else None
        action, target = _install_doc(source, directory, name, csd_root)
        done.append({"directory": directory, "target": target,
                     "action": action, "csdRoot": csd_root})

    import importlib.util
    ready = all(importlib.util.find_spec(mod) is not None for mod in ("numpy", "PIL"))

    payload = {"installed": done, "skipped": skipped,
               "source": source, "dependenciesReady": ready}
    lines = [f"完成 {len(done)} 個專案："]
    for item in done:
        lines.append(f"  {item['action']}  {item['target']}")
        lines.append(f"        .csd 來源: {item['csdRoot'] or '（未設定，agent 會需要你告知）'}")
    if skipped:
        lines += ["", f"略過 {len(skipped)} 個找不到的資料夾："]
        lines += [f"  {d}" for d in skipped]
    if not ready:
        lines += ["", "注意: 相依套件尚未安裝，請到 csdview 資料夾執行 pip install -e ."]
    _emit(payload, args.json, lines, args.pretty)
    return 0


def _ask_source(project: str) -> str | None:
    """問出這個專案的 .csd 來源資料夾，先給自動偵測到的選項。

    這一步不能省：說明檔告訴 agent 怎麼用工具，但沒說要看哪個專案的 .csd，
    agent 就只能反問使用者。
    """
    print()
    print(f"  專案：{project}")
    print("  這個專案要對照哪一份 Cocos Studio 的 .csd？")

    guesses = _detect_source(project)
    # 專案本身找不到就往上一層找，.csd 常和 Creator 專案並排放
    if not guesses:
        parent = os.path.dirname(project)
        if parent and parent != project:
            guesses = [g for g in _detect_source(parent, depth=2)
                       if os.path.normcase(g) != os.path.normcase(project)]

    if guesses:
        print()
        print("  找到這些可能的來源：")
        for index, path in enumerate(guesses, 1):
            print(f"     {index}. {path}   （{_count_csd(path)} 個 .csd）")
        print()
        print("  輸入編號選一個，或直接拖曳／貼上其他資料夾。")
        print("  （直接按 Enter 表示暫時不設定）")
    else:
        print()
        print("  沒有自動找到。請把 cocosstudio 資料夾拖曳到這個視窗，或貼上路徑。")
        print("  （直接按 Enter 表示暫時不設定）")

    while True:
        print()
        try:
            raw = input("  .csd 來源: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        if raw.isdigit() and guesses:
            index = int(raw)
            if 1 <= index <= len(guesses):
                return guesses[index - 1]
            print(f"  請輸入 1 到 {len(guesses)} 之間的編號。")
            continue
        candidate = os.path.abspath(raw)
        if os.path.isdir(candidate):
            found = _count_csd(candidate)
            if found == 0:
                print(f"  這個資料夾底下找不到任何 .csd：{candidate}")
                print("  確定要用它嗎？直接按 Enter 重選，輸入 y 表示確定。")
                try:
                    if input("  > ").strip().lower() != "y":
                        continue
                except (EOFError, KeyboardInterrupt):
                    return None
            return candidate
        print(f"  找不到資料夾：{candidate}")


def cmd_setup(args) -> int:
    """互動式安裝，供 設定AI助手.bat 呼叫。

    中文提示全部在這裡輸出——批次檔本身維持純 ASCII，因為 cmd.exe 解析含中文的
    UTF-8 批次檔會失去同步，把註解切成半截當指令執行。
    """
    print()
    print("  csdview - 讓 AI agent 能檢視 Cocos Studio 畫面")
    print("  ============================================")
    print()

    source = _agent_doc()
    if source is None:
        print("  找不到 dropin/CLAUDE.md，請確認是在 csdview 資料夾內執行。")
        input("  按 Enter 關閉...")
        return 2

    added: list[str] = []

    # 拖進來的資料夾可以有很多個，一次全部處理
    for directory in (args.directory or []):
        directory = os.path.abspath(directory.strip().strip('"').strip("'"))
        if os.path.isdir(directory):
            csd_root = _ask_source(directory)
            action, target = _install_doc(source, directory, args.name, csd_root)
            print(f"  {action}  {target}")
            added.append(directory)
        else:
            print(f"  找不到資料夾，略過：{directory}")

    # 先問要不要把之前設定過的專案一起更新到最新說明
    known = [e for e in _load_registry()
             if os.path.isdir(e["directory"])
             and os.path.abspath(e["directory"]) not in added]
    if known:
        print()
        print(f"  另外有 {len(known)} 個之前設定過的專案：")
        for entry in known[:8]:
            print(f"     {entry['directory']}")
        if len(known) > 8:
            print(f"     ...另外還有 {len(known) - 8} 個")
        print()
        try:
            answer = input("  要一起更新成最新的說明嗎？(Y/n) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            for entry in known:
                # 更新既有專案時沿用原本設定的來源，不再重問
                action, target = _install_doc(source, entry["directory"],
                                              entry.get("name", "CLAUDE.md"),
                                              entry.get("csdRoot"))
                print(f"  {action}  {target}")
                added.append(entry["directory"])

    # 沒有任何來源時才進入互動詢問，可以連續加好幾個
    while not added:
        print()
        print("  請把要讓 AI agent 使用的專案資料夾拖曳到這個視窗，或貼上路徑。")
        print("  通常是你的 Cocos Creator 專案根目錄。一次可以拖多個。")
        print("  （直接按 Enter 結束）")
        print()
        try:
            raw = input("  專案資料夾: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            return 0
        for piece in _split_paths(raw):
            directory = os.path.abspath(piece)
            if os.path.isdir(directory):
                csd_root = _ask_source(directory)
                action, target = _install_doc(source, directory, args.name, csd_root)
                print(f"  {action}  {target}")
                added.append(directory)
            else:
                print(f"  找不到資料夾：{directory}")

    print()
    print(f"  共設定了 {len(added)} 個專案。接下來：")
    print("  1. 在那些專案裡開啟 AI agent（例如 Claude Code）")
    print("  2. 直接說「用 csdview 看某個 .csd 長什麼樣」即可，不必再交代路徑")
    print()
    print("  之後 csdview 更新時，再跑一次這個批次檔就會問你要不要一併更新全部專案。")
    print()
    try:
        input("  按 Enter 關閉...")
    except (EOFError, KeyboardInterrupt):
        pass          # 非互動執行（腳本、CI）時沒有終端可等
    return 0


def _split_paths(raw: str) -> list[str]:
    """把使用者一次貼進來的多個路徑拆開。

    Windows 拖曳多個資料夾會用空白分隔並替含空白的路徑加上引號，
    所以不能直接用空白切。
    """
    out, current, quoted = [], "", False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == " " and not quoted:
            if current:
                out.append(current)
                current = ""
            continue
        current += ch
    if current:
        out.append(current)
    return out


# ---------------------------------------------------------------- serve

def cmd_serve(args) -> int:
    from .server import serve
    serve(root=args.directory, port=args.port, open_browser=not args.no_browser,
          project_root=args.root)
    return 0


# ---------------------------------------------------------------- ui

_UI_REMEMBER = os.path.join(os.path.expanduser("~"), ".csdview_ui")
_UI_KEEP = 8            # 最近開過的資料夾保留幾個


def _remembered_dirs() -> list[str]:
    """網頁介面最近開過的資料夾，最新的在前面。

    刻意不寫進 `~/.csdview_projects`：那份是「設定過 AI 助手的專案」，
    `csdview init --all` 會往裡面每個目錄寫一份 CLAUDE.md——把只是用介面看過的
    資料夾混進去，之後更新說明時就會在不相干的地方生出檔案。

    舊版這個檔只存一行路徑，讀得到就當成只有一筆。
    """
    try:
        with open(_UI_REMEMBER, encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return []
    if not raw:
        return []
    try:
        saved = json.loads(raw)
        if not isinstance(saved, list):
            saved = [saved]
    except ValueError:
        saved = [raw]           # 舊格式：單獨一行路徑
    return [p for p in saved if isinstance(p, str) and p and os.path.isdir(p)]


def _remember_dir(path: str) -> None:
    """把這次用的資料夾放到最近清單的最前面。"""
    path = os.path.abspath(path)
    keep = [path]
    for old in _remembered_dirs():
        if os.path.normcase(old) != os.path.normcase(path):
            keep.append(old)
    try:
        with open(_UI_REMEMBER, "w", encoding="utf-8") as fh:
            json.dump(keep[:_UI_KEEP], fh, ensure_ascii=False, indent=2)
    except OSError:
        pass    # 記不住下次再問一次就好，不值得中斷


def _ui_candidates() -> list[str]:
    """網頁介面可以選的資料夾：最近開過的，加上「設定AI助手」設定過的專案。

    這兩份設定原本各記一份、互不相干，使用者在設定AI助手選好的專案，
    開啟介面卻不知道，只能靠拖曳資料夾切換。這裡把登錄檔一起讀進來當候選。
    """
    found, seen = [], set()
    for path in _remembered_dirs() + [e.get("csdRoot") or e.get("directory") or ""
                                      for e in _load_registry()]:
        if not path or not os.path.isdir(path):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        found.append(os.path.abspath(path))
    return found


def _pick_directory(candidates: list[str]) -> str:
    """列出候選讓使用者挑。Enter 沿用上次那個，也可以直接貼新路徑。

    只有候選超過一個時才問——單一專案的人雙擊之後應該直接進畫面，不該多一步。
    """
    print("  上次用的：%s" % candidates[0])
    print()
    print("  設定過的專案：")
    for i, path in enumerate(candidates, 1):
        mark = "  <- 上次用的" if i == 1 else ""
        print("    %d) %s%s" % (i, path, mark))
    print()
    print("  直接按 Enter 用上次那個；輸入編號切換；")
    print("  或把別的 cocosstudio 資料夾拖曳到這個視窗（也可以貼路徑）。")
    print("  新的資料夾會自動加進這份清單。")
    print()
    while True:
        try:
            answer = input("  選擇: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            return candidates[0]
        if not answer:
            return candidates[0]
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        if os.path.isdir(answer):
            return os.path.abspath(answer)
        print("  這不是編號也不是存在的資料夾，再試一次（Enter 用上次那個）。")

def cmd_ui(args) -> int:
    """互動式啟動網頁介面，供 開啟介面.bat 呼叫。

    所有中文提示都在這裡輸出，批次檔本身保持純 ASCII——cmd.exe 在 UTF-8 模式下
    解析含中文的批次檔會失去同步，把註解切成半截當成指令執行。
    """
    from .server import serve

    print()
    print("  csdview - Cocos Studio .csd 檢視器")
    print("  =================================")
    print()

    if args.directory:
        target = args.directory
    else:
        candidates = _ui_candidates()
        target = (_pick_directory(candidates) if len(candidates) > 1
                  else (candidates[0] if candidates else ""))

    while not target or not os.path.isdir(target):
        if target:
            print(f"  找不到這個資料夾：{target}")
            print()
        print("  請把專案的 cocosstudio 資料夾拖曳到這個視窗，或直接貼上路徑。")
        print(r"  例如：D:\MyGame\ClientCocos\CocoStudio\MyGame\cocosstudio")
        print("  （直接按 Enter 離開）")
        print()
        try:
            target = input("  資料夾: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not target:
            return 0

    target = os.path.abspath(target)
    _remember_dir(target)

    count = sum(1 for _dir, _subs, files in os.walk(target)
                for name in files if name.lower().endswith(".csd"))
    print(f"  資料夾 : {target}")
    print(f"  找到   : {count} 個 .csd 檔案")
    if count == 0:
        print("  這個資料夾裡沒有 .csd，確認一下是不是選錯層了。")
    print()

    serve(root=target, port=args.port, open_browser=not args.no_browser,
          project_root=args.root)
    return 0


# ---------------------------------------------------------------- 工具

# 會影響畫面內容的參數。預設檔名要把它們算進雜湊，否則不同選項會寫到同一個路徑，
# 多個 agent 同時跑時就會互相覆蓋——而且讀到的是別人的圖，據此做出錯誤判斷。
_RENDER_ARGS = (
    "anim", "frame", "time", "scale", "bg", "annotate", "annotate_only", "pad",
    "max_depth", "inner_mode", "font", "no_particles", "full_bounds", "guide",
    "max_pixels", "hide", "show", "root", "extra_root", "count", "cols", "step",
    "labels", "no_full_bounds",
)


def _out_dir() -> str:
    """輸出根目錄。多個 agent 想完全隔離時設 CSDVIEW_OUT 環境變數即可。"""
    return os.environ.get("CSDVIEW_OUT") or "out"


def _option_tag(args) -> str:
    """把影響畫面的參數壓成短雜湊，接在預設檔名後面。

    同樣的指令會得到同樣的路徑（可重複、可快取），不同選項則一定不同路徑。
    """
    parts = []
    for name in _RENDER_ARGS:
        if not hasattr(args, name):
            continue
        value = getattr(args, name)
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        parts.append(f"{name}={value}")
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:8]


def _default_out(csd_path: str, animation: str, frame: float | None,
                 suffix: str = "", ext: str = ".png", args=None) -> str:
    base = os.path.splitext(os.path.basename(csd_path))[0]
    parts = [base]
    if animation:
        parts.append(animation.replace(" ", "_"))
    if frame is not None:
        parts.append(f"f{int(round(frame))}")
    if args is not None:
        parts.append(_option_tag(args))
    return os.path.join(_out_dir(), "_".join(parts) + suffix + ext)


def _atomic_save(save, out: str) -> None:
    """先寫暫存檔再改名。

    改名在同一個檔案系統上是原子操作，所以別的 agent 要嘛讀到完整的舊檔、
    要嘛讀到完整的新檔，不會讀到寫到一半的破圖。
    """
    directory = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=directory, prefix=".csdview-", suffix=os.path.splitext(out)[1],
        delete=False)
    tmp.close()
    try:
        save(tmp.name)
        os.replace(tmp.name, out)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _save_image(image, out: str, **kwargs) -> None:
    if out.lower().endswith((".jpg", ".jpeg")):
        _atomic_save(lambda p: image.convert("RGB").save(p, quality=92, **kwargs), out)
    else:
        _atomic_save(lambda p: image.save(p, **kwargs), out)


def _save(result: RenderResult, out: str) -> None:
    _save_image(result.image, out)


# ---------------------------------------------------------------- 進入點

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="csdview",
        description="Cocos Studio .csd 檢視器：渲染畫面、拖曳時間軸、供 AI agent 讀取。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  csdview info  Deal.csd
  csdview render Deal.csd --anim deal --frame 30 -o out/deal30.png
  csdview render Deal.csd --annotate --json        # 給 agent：圖 + 節點座標
  csdview sheet Deal.csd --anim deal --count 12    # 一張圖看完整段動畫
  csdview tree  Deal.csd --frame 30
  csdview find  Deal.csd --name btn --json
  csdview gif   Deal.csd --anim deal --step 2
  csdview doctor Deal.csd
  csdview serve .                                  # 開啟網頁介面
""")
    subs = ap.add_subparsers(dest="command", required=True)

    p = subs.add_parser("info", help="顯示檔案結構、動畫清單與統計")
    _add_common(p, with_render=False)
    p.set_defaults(func=cmd_info)

    p = subs.add_parser("render", help="渲染單一影格成 PNG")
    _add_common(p)
    p.add_argument("--out", "-o", help="輸出檔案路徑（預設 out/<名稱>_f<影格>.png）")
    p.set_defaults(func=cmd_render)

    p = subs.add_parser("sheet", help="把一段動畫平鋪成一張總覽圖")
    _add_common(p)
    p.add_argument("--out", "-o", help="輸出檔案路徑")
    p.add_argument("--count", "-n", type=int, default=9, help="取樣格數（預設 9）")
    p.add_argument("--cols", type=int, default=3, help="每列幾格（預設 3）")
    p.add_argument("--no-labels", dest="labels", action="store_false",
                   help="不要標上影格編號")
    p.add_argument("--jobs", type=int, default=0,
                   help="並行渲染的執行緒數，0 為依畫布大小自動決定")
    p.set_defaults(func=cmd_sheet)

    p = subs.add_parser("frames", help="批次輸出每一格 PNG")
    _add_common(p)
    p.add_argument("--out", "-o", help="輸出資料夾（預設 <輸出根目錄>/frames_<選項雜湊>）")
    p.add_argument("--step", type=int, default=1, help="每隔幾格輸出一張")
    p.add_argument("--jobs", type=int, default=0,
                   help="並行渲染的執行緒數，0 為依畫布大小自動決定")
    p.set_defaults(func=cmd_frames)

    p = subs.add_parser(
        "gif", help="輸出動畫 GIF（播放速度為近似值，GIF 的延遲單位是 1/100 秒）")
    _add_common(p)
    p.add_argument("--out", "-o", help="輸出檔案路徑")
    p.add_argument("--step", type=int, default=2,
                   help="每隔幾格取一張（預設 2）。GIF 只能存 10ms 的倍數，"
                        "step 越小速度誤差越大；要精確逐格看請用 frames 或 sheet")
    p.add_argument("--jobs", type=int, default=0,
                   help="並行渲染的執行緒數，0 為依畫布大小自動決定")
    p.set_defaults(func=cmd_gif)

    p = subs.add_parser("tree", help="列出節點階層與畫面上的位置")
    _add_common(p)
    p.add_argument("--depth", type=int, default=99, help="顯示層數上限")
    p.set_defaults(func=cmd_tree)

    p = subs.add_parser("find", help="依名稱搜尋節點並回報畫面座標")
    _add_common(p)
    p.add_argument("--name", "-n", default="", help="名稱或路徑關鍵字")
    p.add_argument("--type", help="節點類型關鍵字，例如 Sprite、Button")
    p.set_defaults(func=cmd_find)

    p = subs.add_parser("compare", help="與參考圖（Cocos Creator 截圖）比對差異")
    _add_common(p)
    p.add_argument("reference", help="參考圖片路徑")
    p.add_argument("--out", "-o", help="三格對照圖的輸出路徑")
    p.add_argument("--threshold", type=float, default=0.05,
                   help="判定為差異的像素門檻 0~1（預設 0.05）")
    p.add_argument("--diff-bg", default="#000000",
                   help="比對時透明區域疊的底色（兩張圖要一致）")
    p.add_argument("--top", type=int, default=15, help="列出差異最大的前幾個節點")
    p.add_argument("--all-nodes", action="store_true",
                   help="連容器節點一起列（預設只列葉節點）")
    p.add_argument("--no-diff-image", action="store_true", help="不輸出對照圖")
    p.set_defaults(func=cmd_compare)

    p = subs.add_parser("spec", help="匯出節點規格 JSON，供移植後結構化比對")
    _add_common(p)
    p.add_argument("--out", "-o", help="輸出檔案路徑（省略則印到 stdout）")
    p.add_argument("--node", help="只匯出這個節點與它的子孫（節點路徑，例如 Layer/panel）")
    p.add_argument("--depth", type=int, default=99,
                   help="深度上限；配合 --node 時從該節點算第 0 層")
    p.set_defaults(func=cmd_spec)

    p = subs.add_parser("doctor", help="檢查缺少的資源")
    _add_common(p, with_render=False)
    p.set_defaults(func=cmd_doctor)

    p = subs.add_parser("list", help="列出資料夾底下所有 .csd")
    p.add_argument("directory", nargs="?", default=".", help="要掃描的資料夾")
    p.add_argument("--detail", action="store_true", help="一併解析動畫資訊")
    p.add_argument("--json", action="store_true", help="以 JSON 輸出")
    p.add_argument("--pretty", action="store_true",
                   help="JSON 加上縮排（給人看；預設是緊湊輸出，省 token）")
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("ui", help="互動式啟動網頁檢視器（開啟介面.bat 用的進入點）")
    p.add_argument("directory", nargs="?", help="要瀏覽的資料夾；省略則沿用上次的選擇或詢問")
    p.add_argument("--port", type=int, default=8770, help="連接埠（預設 8770）")
    p.add_argument("--root", help="專案 cocosstudio 根目錄（預設自動偵測）")
    p.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    p.set_defaults(func=cmd_ui)

    p = subs.add_parser("init", help="把 agent 說明檔安裝到一個或多個專案")
    p.add_argument("directory", nargs="*",
                   help="目標專案的根目錄，可一次給多個")
    p.add_argument("--all", action="store_true",
                   help="更新所有設定過的專案（說明檔有更新時用這個補齊）")
    p.add_argument("--list", action="store_true", help="列出設定過的專案")
    p.add_argument("--source", metavar="cocosstudio資料夾",
                   help="這個專案的 .csd 來源；省略則自動偵測")
    p.add_argument("--name", default="CLAUDE.md",
                   help="要寫入的檔名（預設 CLAUDE.md；其他工具可改成 AGENTS.md 等）")
    p.add_argument("--json", action="store_true", help="以 JSON 輸出")
    p.add_argument("--pretty", action="store_true",
                   help="JSON 加上縮排（給人看；預設是緊湊輸出，省 token）")
    p.set_defaults(func=cmd_init)

    p = subs.add_parser("setup", help="互動式安裝（設定AI助手.bat 用的進入點）")
    p.add_argument("directory", nargs="*",
                   help="目標專案資料夾，可一次拖多個；省略則詢問")
    p.add_argument("--name", default="CLAUDE.md", help="要寫入的檔名")
    p.set_defaults(func=cmd_setup)

    p = subs.add_parser("serve", help="啟動網頁檢視器")
    p.add_argument("directory", nargs="?", default=".", help="要瀏覽的資料夾")
    p.add_argument("--port", type=int, default=8770, help="連接埠（預設 8770）")
    p.add_argument("--root", help="專案 cocosstudio 根目錄（預設自動偵測）")
    p.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    p.set_defaults(func=cmd_serve)

    return ap


def main(argv: list[str] | None = None) -> int:
    # Windows 主控台預設不是 UTF-8，中文與 JSON 都會亂碼。
    # 一併開行緩衝：輸出被導向檔案或管道時（agent 擷取輸出、批次檔記 log）
    # 才不會卡在區塊緩衝裡看不到進度。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass

    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, ValueError, OSError) as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(file=sys.stderr)
        print("已中斷。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
