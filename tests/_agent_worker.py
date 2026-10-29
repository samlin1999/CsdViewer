"""一個「agent」：獨立行程，呼叫一次 csd MCP 工具，把結果印成一行 JSON。

跟真實情況一致——每個 Claude Code 視窗就是一個獨立的 MCP server 行程。
"""
import json
import sys

# 專案路徑由呼叫端用 PYTHONPATH 傳進來，這裡不寫死


from csdmcp import tools

name = sys.argv[1]
args = json.loads(sys.argv[2])
try:
    payload, _image = tools.call(name, args)
    print(json.dumps({"ok": True, "payload": payload},
                     ensure_ascii=False, default=str))
except Exception as exc:
    print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)},
                     ensure_ascii=False))
