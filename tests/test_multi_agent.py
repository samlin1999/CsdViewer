"""多個 agent 同時使用會不會出問題。

    python tests/test_multi_agent.py <某個專案的 .ccs>

每個 agent 是一個獨立行程——跟實際情況一致：一個 Claude Code 視窗就是一個
獨立的 MCP server 行程。共用的可寫狀態逐一壓測：`.ccs` 專案檔、
`~/.csdview_projects` 登錄檔、同一份 `.csd`、MCP 的暫存圖資料夾、
行程內的 atlas 快取，以及網頁介面的埠號。

登錄檔用 `CSDVIEW_PROJECTS` 環境變數導到暫存目錄，不會動到使用者真正的那一份。
找不到 csdmcp 時，需要它的那幾項會跳過。
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CSDVIEW = os.path.dirname(HERE)
sys.path.insert(0, CSDVIEW)
WORKER = os.path.join(HERE, "_agent_worker.py")

REAL_CCS = sys.argv[1] if len(sys.argv) > 1 else ""
if not os.path.isfile(REAL_CCS):
    print("用法: python tests/test_multi_agent.py <某個專案的 .ccs>")
    sys.exit(2)

MCP = ""
for base in (os.environ.get("CSDMCP_HOME"),
             os.path.join(os.path.dirname(CSDVIEW), "Mangan_git", "mcpServer")):
    if base and os.path.isdir(os.path.join(base, "csdmcp")):
        MCP = base
        break

results = []
def check(name, ok, extra=""):
    results.append(bool(ok))
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("   " + extra) if extra else ""))

work = tempfile.mkdtemp()
home = os.path.join(work, "home"); os.makedirs(home)
proj = os.path.join(work, "proj")
root = os.path.join(proj, "cocosstudio", "probe")
os.makedirs(root)
shutil.copy(REAL_CCS, os.path.join(proj, "Inanna.ccs"))
CSDROOT = os.path.join(proj, "cocosstudio")

REGISTRY = os.path.join(home, "projects.json")
ENV = dict(os.environ, CSDVIEW_PROJECTS=REGISTRY,
           PYTHONIOENCODING="utf-8", PYTHONPATH=MCP + os.pathsep + CSDVIEW)


def agents(calls):
    """同時啟動 N 個行程，等全部結束，回傳各自的結果。"""
    procs = [subprocess.Popen(
        [sys.executable, WORKER, name, json.dumps(args, ensure_ascii=False)],
        cwd=work, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for name, args in calls]
    out = []
    for p in procs:
        so, se = p.communicate(timeout=300)
        try:
            out.append(json.loads(so.decode("utf-8").strip().splitlines()[-1]))
        except Exception:
            out.append({"ok": False, "error": "輸出不是 JSON: "
                        + (so.decode("utf-8", "replace")[:80]
                           or se.decode("utf-8", "replace")[-160:])})
    return out


# ---------------------------------------------------------------- A
if not MCP:
    print("SKIP  這台機器上找不到 csdmcp，A~D、F 跳過")
else:
    print("（以下 A~D、F 需要 csdmcp）")

print("0. CSDVIEW_PROJECTS 兩邊都要認得（不然登錄檔會分家）")
seen = []
for code in ("from csdview import cli; print(cli._REGISTRY)",
             "from csdmcp import projects; print(projects.REGISTRY)"):
    r = subprocess.run([sys.executable, "-c", code], env=ENV,
                       capture_output=True, text=True)
    seen.append(r.stdout.strip())
check("csdview 與 csdmcp 指到同一份登錄檔",
      seen[0] == seen[1] == REGISTRY if MCP else seen[0] == REGISTRY,
      " / ".join(seen))

print()
print("A. 6 個 agent 同時 create_csd（都要登錄進同一個 .ccs）")
calls = [("create_csd", {"path": os.path.join(root, "a%d.csd" % i),
                         "rootType": "layer", "width": 100, "height": 100,
                         "project": CSDROOT}) for i in range(6)]
res = agents(calls)
bad = [r.get("error") for r in res if not r.get("ok")]
check("6 個都成功", not bad, "; ".join(map(str, bad))[:110])
ccs = os.path.join(proj, "Inanna.ccs")
try:
    tree = ET.parse(ccs)
    names = {el.get("Name") for el in tree.iter("Project")}
    ok_xml = True
except ET.ParseError as exc:
    names, ok_xml = set(), False
    print("      .ccs 壞了:", exc)
check(".ccs 仍是合法 XML", ok_xml)
want = {"a%d.csd" % i for i in range(6)}
check("6 筆登錄一個都沒掉", want <= names, "缺 %s" % sorted(want - names))
check("沒有留下 .tmp 殘骸",
      not [f for f in os.listdir(proj) if f.endswith(".tmp")])

# ---------------------------------------------------------------- B
print()
print("B. 6 個 agent 同時 add_project（同一份登錄檔）")
dirs = []
for i in range(6):
    d = os.path.join(work, "p%d" % i, "cocosstudio")
    os.makedirs(d)
    dirs.append(d)
res = agents([("add_project", {"directory": d}) for d in dirs])
bad = [r.get("error") for r in res if not r.get("ok")]
check("6 個都成功", not bad, "; ".join(map(str, bad))[:110])
reg = REGISTRY
try:
    entries = json.load(open(reg, encoding="utf-8"))
    ok_json = True
except Exception as exc:
    entries, ok_json = [], False
    print("      登錄檔壞了:", exc)
check("登錄檔仍是合法 JSON", ok_json)
roots = {os.path.normcase(e.get("csdRoot") or "") for e in entries}
missing = [d for d in dirs if os.path.normcase(d) not in roots]
check("6 筆專案一個都沒掉", not missing, "缺 %d 筆" % len(missing))

# ---------------------------------------------------------------- C
print()
print("C. 6 個 agent 同時改 6 個不同的 .csd")
res = agents([("set_prop", {"csd": os.path.join(root, "a%d.csd" % i),
                            "node": "Layer", "props": {"alpha": 200 - i},
                            "project": CSDROOT}) for i in range(6)])
bad = [r.get("error") for r in res if not r.get("ok")]
check("6 個都成功", not bad, "; ".join(map(str, bad))[:110])
sys.path.insert(0, CSDVIEW)
from csdview.parser import parse
alphas = []
for i in range(6):
    d = parse(os.path.join(root, "a%d.csd" % i))
    alphas.append(round(d.root.alpha))
check("每個檔都解析得動且值正確", alphas == [200 - i for i in range(6)], str(alphas))

# ---------------------------------------------------------------- D
print()
print("D. 6 個 agent 同時改「同一個」.csd（樂觀檢查該擋掉大部分）")
target = os.path.join(root, "a0.csd")
before = open(target, "rb").read()
res = agents([("set_prop", {"csd": target, "node": "Layer",
                            "props": {"alpha": 10 + i}, "project": CSDROOT})
              for i in range(6)])
won = [r for r in res if r.get("ok")]
lost = [r for r in res if not r.get("ok")]
check("至少有一個成功", len(won) >= 1, "成功 %d / 失敗 %d" % (len(won), len(lost)))
check("失敗的都是樂觀檢查擋下來的，不是當掉",
      all("被別的程式改過" in str(r.get("error")) or "改過" in str(r.get("error"))
          for r in lost) if lost else True,
      "; ".join(str(r.get("error"))[:60] for r in lost[:2]))
try:
    d = parse(target)
    val = round(d.root.alpha)
    ok_parse = True
except Exception as exc:
    val, ok_parse = None, False
check("檔案沒有壞", ok_parse, "alpha=%s" % val)
check("內容是某一個 agent 寫的完整版本，不是混合",
      val in [10 + i for i in range(6)], "alpha=%s" % val)
check("沒有留下 .tmp 殘骸",
      not [f for f in os.listdir(root) if f.endswith(".tmp")])

# ---------------------------------------------------------------- E
print()
print("E. 同一個行程內多執行緒共用 atlas 快取")
from csdview.assets import atlas_map
errors, tables = [], []
def hammer():
    try:
        tables.append(atlas_map(CSDROOT))
    except Exception as exc:
        errors.append("%s: %s" % (type(exc).__name__, exc))
ths = [threading.Thread(target=hammer) for _ in range(16)]
for t in ths: t.start()
for t in ths: t.join()
check("16 條執行緒都沒有例外", not errors, "; ".join(errors[:2]))
check("拿到的是同一份快取物件（沒有重複建表）",
      len(tables) == 16 and all(t is tables[0] for t in tables))

# ---------------------------------------------------------------- F
print()
print("F. 8 個 agent 同時 render（MCP 的暫存圖資料夾會互相清舊檔）")
res = agents([("render", {"csd": os.path.join(root, "a%d.csd" % (i % 6)),
                          "project": CSDROOT, "scale": 1})
              for i in range(8)])
bad = [r.get("error") for r in res if not r.get("ok")]
check("8 個都成功（沒有被別人清掉圖）", not bad, "; ".join(map(str, bad))[:110])
imgs = [r["payload"].get("image") for r in res if r.get("ok")]
check("每個 agent 都拿到自己的圖檔路徑", len(imgs) == 8 and all(imgs))
alive = [p for p in imgs if p and os.path.isfile(p) and os.path.getsize(p) > 100]
check("圖檔都還在且不是空的", len(alive) == len(imgs),
      "%d / %d" % (len(alive), len(imgs)))

# ---------------------------------------------------------------- G
print()
print("G. 兩個 agent 同時開網頁介面（預設埠一樣）")
sites = []
for tag in ("A", "B"):
    d = os.path.join(work, "site" + tag, "cocosstudio")
    os.makedirs(d)
    shutil.copy(os.path.join(root, "a0.csd"), os.path.join(d, "only_in_%s.csd" % tag))
    sites.append(d)
procs = []
for d in sites:
    procs.append(subprocess.Popen(
        [sys.executable, "-m", "csdview", "serve", d, "--port", "8931", "--no-browser"],
        env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    time.sleep(4)

def files_at(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/files" % port, timeout=20) as r:
        return [f["path"] for f in json.loads(r.read())["files"]]

try:
    got = {}
    for port, tag in ((8931, "A"), (8932, "B")):
        try:
            got[tag] = files_at(port)
        except Exception as exc:
            got[tag] = "查不到: %s" % exc
    check("第二個 agent 會自動換到下一個埠", isinstance(got.get("B"), list),
          str(got.get("B"))[:70])
    check("兩邊各自看到自己的專案，不會拿到對方的",
          isinstance(got.get("A"), list) and isinstance(got.get("B"), list)
          and any("only_in_A" in f for f in got["A"])
          and any("only_in_B" in f for f in got["B"]),
          "A=%s B=%s" % (got.get("A"), got.get("B")))
finally:
    for p in procs:
        p.terminate()
        try: p.wait(timeout=8)
        except Exception: p.kill()

shutil.rmtree(work, ignore_errors=True)
print()
print("%d/%d 通過" % (sum(1 for r in results if r), len(results)))
sys.exit(0 if all(results) else 1)
