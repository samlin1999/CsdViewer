"""工具之間的交互與競態回歸測試。

    python tests/test_interop.py <某個 .csd>

同一批 .csd 平常會被四個東西碰到：MCP 編輯器（可能有好幾個行程）、csdview CLI
（多個 agent 各自一個行程）、csdview serve，偶爾還有 Cocos Studio。
這裡測的是它們同時動作時會不會互相弄壞。

找不到 csdmcp 時會跳過需要它的那幾項，其餘照跑。
"""
import contextlib, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile
import threading, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from csdview import server as S
from csdview.parser import parse

try:
    from csdmcp import editor
except ImportError:
    editor = None

SRC = sys.argv[1] if len(sys.argv) > 1 else None
if not SRC or not os.path.isfile(SRC):
    print("用法: python tests/test_interop.py <某個 .csd>")
    sys.exit(2)

results = []
def check(name, ok, extra=""):
    results.append(bool(ok))
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("   " + extra) if extra else ""))

def fresh_tree():
    work = tempfile.mkdtemp()
    root = os.path.join(work, "cocosstudio")
    shutil.copytree(os.path.dirname(SRC), os.path.join(root, "part"))
    return work, os.path.join(root, "part", os.path.basename(SRC))

_PAT = re.compile(r'<Scale ScaleX="[\d.]+" ScaleY="[\d.]+"')
def variant(text, value):
    return _PAT.sub('<Scale ScaleX="%.4f" ScaleY="%.4f"' % (value, value), text)

def atomic_write(path, body, attempts=6):
    """原子替換，撞到別人開著檔就短暫重試。

    Windows 上 os.replace 遇到「目標正被別的行程開著」會直接 PermissionError
    （Python 開檔沒帶 FILE_SHARE_DELETE）。實測有人在不停解析同一個檔時，
    不重試只有 5/60 次寫得進去，加上重試是 60/60。
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    os.write(fd, body.encode("utf-8") if isinstance(body, str) else body)
    os.close(fd)
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                os.unlink(tmp)
                raise
            time.sleep(0.02 * (i + 1))


# ---------------------------------------------------------------- 0
print("0. 解析途中檔案被換掉，伺服器的文件快取要能自我修正")
work = tempfile.mkdtemp()
try:
    p = os.path.join(work, "T.csd")
    shutil.copy(SRC, p)
    text = io.open(p, encoding="utf-8").read()
    atomic_write(p, variant(text, 1.0))

    state = S.ViewerState(work)
    real_parse = S.csd_parser.parse
    calls = {"n": 0}
    def racing_parse(path):
        doc = real_parse(path)
        if calls["n"] == 0:
            atomic_write(p, variant(text, 2.5))   # 解析完、還沒進快取之前被換掉
        calls["n"] += 1
        return doc
    S.csd_parser.parse = racing_parse
    try:
        state.document(p)
        second = state.document(p)
    finally:
        S.csd_parser.parse = real_parse

    got = 2.5 if any(abs(n.scale_x - 2.5) < 1e-6 for n in second.root.walk()) else 1.0
    check("第二次會重新解析", calls["n"] == 2, "解析 %d 次" % calls["n"])
    check("拿到的是檔案現在的內容", got == 2.5,
          "scale=%.1f（檔案裡是 2.5）" % got)
finally:
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 0b
print()
print("0b. 影格快取要同時受張數與位元組上限約束")
c = S._Cache(limit=240, limit_bytes=1_000_000)
for i in range(300):
    c.put(("k", i), b"x" * 300_000)
n, b = c.stats()
check("超過位元組上限會淘汰", b <= 1_000_000, "留下 %d 張 / %.2f MB" % (n, b / 1e6))
c2 = S._Cache(limit=240, limit_bytes=1_000_000)
c2.put(("big",), b"y" * 5_000_000)
check("單張超過上限也不會把自己踢掉", c2.stats()[0] == 1)
c3 = S._Cache(limit=3, limit_bytes=10**9)
for i in range(10):
    c3.put((i,), b"z" * 10)
check("張數上限仍然有效", c3.stats()[0] == 3)
c4 = S._Cache(limit=10, limit_bytes=10**9)
c4.put(("a",), b"1" * 100)
c4.put(("a",), b"2" * 500)
check("同鍵覆寫時位元組計數正確", c4.stats()[1] == 500, "%d bytes" % c4.stats()[1])

# ---------------------------------------------------------------- A
print()
print("A. 兩個 MCP 同時改同一個 .csd（樂觀檢查該擋下其中一個）")
if editor is None:
    print("  SKIP  這台機器上沒有 csdmcp")
else:
    work, csd = fresh_tree()
    try:
        a = editor.Csd(csd)
        b = editor.Csd(csd)                   # 兩邊在同一時間各讀進來一份
        a.xml.data = a.xml.data.replace(b'<Scale ScaleX="1.0000"',
                                        b'<Scale ScaleX="3.0000"', 1)
        b.xml.data = b.xml.data.replace(b'<Scale ScaleX="1.0000"',
                                        b'<Scale ScaleX="7.0000"', 1)
        first = a.save("A 的修改")
        err = None
        try:
            b.save("B 的修改")
        except editor.EditError as exc:
            err = str(exc)
        check("先寫的成功", first.get("written") is True)
        check("後寫的被擋下，不會無聲蓋掉", err is not None)
        content = open(csd, "rb").read()
        check("檔案裡是先寫的那份",
              b'ScaleX="3.0000"' in content and b'ScaleX="7.0000"' not in content)
        check("檔案仍然解析得動", parse(csd) is not None)
        check("沒有留下 .tmp 殘骸",
              not [f for f in os.listdir(os.path.dirname(csd)) if f.endswith(".tmp")])
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- B
print()
print("B. 有人正在讀，同時有人原子寫入")
work, csd = fresh_tree()
try:
    text = io.open(csd, encoding="utf-8").read()
    stop = threading.Event()
    reads = {"ok": 0, "bad": 0, "first": ""}

    def reader():
        while not stop.is_set():
            try:
                doc = parse(csd)
                assert sum(1 for _ in doc.root.walk()) > 0
                reads["ok"] += 1
            except PermissionError:
                pass                          # 正好撞上替換，parse 自己會重試
            except Exception as exc:
                reads["bad"] += 1
                if not reads["first"]:
                    reads["first"] = "%s: %s" % (type(exc).__name__, exc)

    th = threading.Thread(target=reader, daemon=True)   # 主執行緒炸了也不能卡住
    th.start()
    time.sleep(0.2)
    blocked = 0
    try:
        for i in range(30):
            try:
                atomic_write(csd, variant(text, 1 + i * 0.03))
            except PermissionError:
                blocked += 1
            time.sleep(0.03)
    finally:
        stop.set()
        th.join(timeout=10)
    # 這裡的讀者是不間斷的解析迴圈，比現實嚴苛得多（真的在看的人 1.5 秒才問一次）。
    # 所以「被擋下幾次」只當成參考數字，不當成通過條件；真正要保證的是內容不會壞。
    print("      （參考）最壞情況下寫入被擋下 %d / 30" % blocked)
    check("讀到的內容從不損壞", reads["bad"] == 0,
          "成功 %d 次，壞掉 %d 次%s"
          % (reads["ok"], reads["bad"], "  例: " + reads["first"][:70] if reads["first"] else ""))
finally:
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- B2
print()
print("B2. 真實節奏：看的人每 200ms 讀一次，同時有人在寫")
work, csd = fresh_tree()
try:
    text = io.open(csd, encoding="utf-8").read()
    stop = threading.Event()
    reads = {"ok": 0, "bad": 0}

    def slow_reader():
        while not stop.is_set():
            try:
                parse(csd)
                reads["ok"] += 1
            except PermissionError:
                pass
            except Exception:
                reads["bad"] += 1
            stop.wait(0.2)

    th = threading.Thread(target=slow_reader, daemon=True)
    th.start()
    blocked = 0
    try:
        for i in range(20):
            try:
                atomic_write(csd, variant(text, 1 + i * 0.05))
            except PermissionError:
                blocked += 1
            time.sleep(0.1)
    finally:
        stop.set()
        th.join(timeout=10)
    check("寫入完全不會被擋下", blocked == 0, "被擋下 %d / 20" % blocked)
    check("讀到的內容從不損壞", reads["bad"] == 0,
          "成功讀 %d 次" % reads["ok"])
finally:
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- C
print()
print("C. 多個 agent 各自跑 CLI，輸出不可以互相蓋掉")
# 這一段不會改 .csd，只是同時渲染六個影格，所以直接讀原始檔。
# 複製到暫存樹的話貼圖會留在原地（source 是相對 cocosstudio 根的路徑，
# fresh_tree 只搬了 .csd 那一層），六張圖全渲染成空白，反而驗不到內容。
# 輸出位置由 cwd 與 CSDVIEW_OUT 決定，仍然完全落在暫存區，不會污染專案。
work, csd = tempfile.mkdtemp(), SRC
try:
    procs = []
    for i in range(6):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        if i % 2:                             # 一半的 agent 自己設隔離目錄
            env["CSDVIEW_OUT"] = os.path.join(work, "agent%d" % i)
        else:
            env.pop("CSDVIEW_OUT", None)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "csdview", "render", csd, "--json",
             "--frame", str(i)],
            cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))

    paths, errs = [], []
    for i, pr in enumerate(procs):
        out, err = pr.communicate(timeout=300)
        if pr.returncode != 0:
            errs.append("agent%d 離開碼 %d: %s"
                        % (i, pr.returncode, err.decode("utf-8", "replace")[:80]))
            continue
        try:
            paths.append(json.loads(out.decode("utf-8"))["image"])
        except ValueError as exc:
            errs.append("agent%d 的 stdout 不是 JSON: %s" % (i, exc))

    full = [os.path.join(work, x) for x in paths]
    check("每個 agent 都成功", len(paths) == 6 and not errs, "; ".join(errs)[:90])
    check("輸出檔名各不相同", len(set(paths)) == len(paths),
          "%d 個路徑 / %d 個相異" % (len(paths), len(set(paths))))
    check("每張圖都存在且不是空的",
          all(os.path.isfile(f) and os.path.getsize(f) > 1000 for f in full))
    digests = [hashlib.md5(open(f, "rb").read()).hexdigest() for f in full]
    # 「六張圖要不一樣」只有在這個檔真的有動畫時才成立。靜態 .csd 的六個影格
    # 本來就該長得一模一樣——那時要驗的是反過來：同樣的輸入給同樣的輸出。
    # 寫死成「必須不同」的話，餵一個沒有時間軸的檔就會誤報成 csdview 的錯。
    animated = (parse(csd).duration or 0) > 0
    if animated:
        check("不同影格畫出來的內容真的不同", len(set(digests)) == len(digests),
              "%d 個相異" % len(set(digests)))
    else:
        check("靜態檔的每個影格都畫出同一張圖", len(set(digests)) == 1,
              "%d 個相異（這個 .csd 沒有時間軸）" % len(set(digests)))
finally:
    shutil.rmtree(work, ignore_errors=True)


print()
print("%d/%d 通過" % (sum(1 for r in results if r), len(results)))
sys.exit(0 if all(results) else 1)
