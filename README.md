# csdview

讀取 Cocos Studio 的 `.csd` 檔案，在 Python 端把畫面渲染出來，可以拖曳時間軸看每一格，
也可以由 AI agent 透過指令列呼叫——特別是拿來驗證「移植到 Cocos Creator 後畫面對不對」。

人在網頁上看到的畫面，跟 agent 用 CLI 取得的 PNG，是同一份渲染程式碼算出來的，
所以兩邊不會有落差。

## 給團隊使用：兩個批次檔

| 雙擊這個 | 用途 |
| --- | --- |
| **`設定AI助手.bat`** | 讓某個專案的 AI agent 學會用 csdview（第一次要做一次） |
| **`開啟介面.bat`** | 開啟網頁檢視器，人自己看畫面 |

兩個都會在首次執行時自動安裝相依套件，不需要先手動裝任何東西。

`設定AI助手.bat` 會問兩件事：

1. **要設定哪個專案**（Cocos Creator 專案資料夾，可一次拖曳多個）
2. **這個專案要對照哪份 `.csd`**（會自動偵測附近的 `cocosstudio` 資料夾讓你選編號）

第二項會寫進產生的 `CLAUDE.md`，所以之後對 AI agent 說「用 csdview 看某個 .csd」
就不必再交代來源在哪。設定過的專案會被記住，之後 csdview 更新時再跑一次批次檔，
它會問你要不要把全部專案一起更新到最新說明。

等效的指令：

```bash
csdview init <專案A> <專案B> --source <cocosstudio資料夾>
csdview init --list        # 看設定過哪些專案
csdview init --all         # 全部更新到最新說明
```

設定過的專案記在 `~/.csdview_projects`，csdview 與 csd MCP 共用同一份。
要換一個位置（測試、CI，或想讓不同工作環境各自獨立）就設 `CSDVIEW_PROJECTS`
環境變數指向那個檔案——**兩邊都認得同一個變數**，不會分家。

## 安裝

```bash
pip install -e .
```

之後在任何目錄都能直接使用 `csdview` 指令。這一步也讓文件與設定檔完全不需要寫死路徑，
搬動資料夾或換機器都不用改。

不想安裝也可以用 `python -m csdview <指令>`，或在專案資料夾內直接跑 `python csdview.py <指令>`。

不需要 Cocos Studio、Cocos2d-x 或任何引擎執行檔，相依套件只有 Pillow 與 numpy。

## 網頁介面（給人看）

雙擊 **`開啟介面.bat`** 就會啟動。第一次執行會請你指定專案的 `cocosstudio` 資料夾
（可以直接把資料夾拖曳到視窗上）。

**要切換專案**：設定過一個以上的專案時，啟動時會列出來讓你選——

```
  上次用的：D:\GameA\...\cocosstudio

  設定過的專案：
    1) D:\GameA\...\cocosstudio  <- 上次用的
    2) D:\GameB\...\cocosstudio

  直接按 Enter 用上次那個；輸入編號切換；
  或把別的 cocosstudio 資料夾拖曳到這個視窗（也可以貼路徑）。
```

**要新增專案到清單**：直接把新的 `cocosstudio` 資料夾拖進那個視窗（或貼路徑）就好，
下次啟動它就在清單裡了。最近開過的會記在 `~/.csdview_ui`，保留 8 個、由新到舊排序。

清單同時包含 `設定AI助手.bat` 設定過的專案（`~/.csdview_projects`），所以那邊設過的
這裡就看得到。兩份刻意分開存：`csdview init --all` 會往登錄檔裡每個專案寫一份
`CLAUDE.md`，只是用介面看過的資料夾混進去，之後更新說明就會在不相干的地方生出檔案。

只有一個專案時不會多問這一步，雙擊就直接進畫面。
也可以把資料夾拖到 `開啟介面.bat` 的圖示上直接開那一個（同樣會加進清單）。

首次執行若缺少 Pillow／numpy，批次檔會自動安裝。

指令列的等效寫法：

```bash
csdview serve <cocosstudio 資料夾>
```

埠被占用時會自動往上找下一個並告訴你——多個人（或多個 agent）各開一個專案時
不必手動喬埠號。Windows 上這一步是必要的：預設的 socket 選項允許兩個行程綁同一個埠，
第二個不會報錯但請求全被第一個接走，畫面會是別人的專案。

瀏覽器會自動開啟 <http://127.0.0.1:8770/>：

- 左側：`.csd` 檔案列表，可篩選
- 中間：渲染畫面，可切換背景（格子／透明／純色）與縮放
- 下方：動畫下拉選單、播放鍵、時間軸滑桿、逐格輸入框，以及「畫布外內容」開關
  （預設開啟；不少 `.csd` 會把元件先擺在設計區外，關掉就只看設計區）
- 右側：節點階層，滑鼠停留可看座標、資源路徑、引用的 `.csd`；
  每個節點前的圓點可切換可視狀態（照 `.csd` → 強制顯示 → 強制隱藏 → 循環），
  「重設可視」清除全部覆寫

快捷鍵：<kbd>空白鍵</kbd> 播放／暫停、<kbd>←</kbd><kbd>→</kbd> 逐格（按住 Shift 跳 10 格）、
<kbd>Home</kbd>／<kbd>End</kbd> 跳到頭尾。

整段動畫的影格會在背景預先抓好，所以拖曳時間軸是即時的。

### 自動更新

`.csd` 被改動時畫面會自動重畫，不必手動重選檔案——**AI agent 在編輯時，人可以在旁邊即時看到結果**。
不需要開著 Cocos Studio：改檔的是誰都一樣，MCP 或指令稿直接寫檔也會觸發。

前端每 1.5 秒問一次 `/api/stat`（只回檔案指紋，不解析內容，實測約 7ms），
指紋是**奈秒級 mtime 加檔案大小**。這裡刻意不用整秒：agent 改檔時一秒內連改好幾次是常態，
實測同一秒內改 6 次，取整秒只看得到 3 次，用奈秒則 6 次都抓得到。

偵測到改動後**不會馬上重載，而是等檔案安定下來**——指紋連續兩次相同才動作
（等待期間改用 400ms 的密集輪詢，最多等 8 秒）。理由有兩個：agent 改一個版面
通常是連續好幾次寫檔，中途重載只會畫出看不到的中間狀態；而且 Windows 上開著檔案
會讓對方的原子替換（`os.replace`）失敗，等它寫完就完全不會互相干擾。
實測 12 次連續寫檔（每 50ms），對方寫檔失敗 0 次、重載 1 次、拿到的是最後的版本。

分頁切到背景時停止輪詢，切回來立刻補一次。控制列的「自動更新」可以關掉。

重載失敗時（例如正好讀到被換掉一半的檔）不會把新指紋記下來，下一輪會再試，
所以不會卡在舊畫面。

貼圖或字型被替換時 `.csd` 的 mtime 不會變，這種情況按「重新整理」即可——
它會換一組版本參數，把所有素材一併重抓。

影格圖片標成可長期快取，網址的版本參數包含 `.csd` 的 mtime、伺服器開機序號與重整序號，
所以更新 csdview 後重啟 serve 就會生效，不需要清瀏覽器快取。

## 指令列（給 AI agent）

每個子指令都支援 `--json`，輸出結構穩定、可直接解析。圖片一律寫成檔案並在 JSON 裡回報路徑。

JSON 預設是**緊湊輸出**——縮排對解析沒有幫助，但對 AI agent 來說很貴：
實測 `tree`／`spec` 光是拿掉縮排就少了六成 token。要自己看時加 `--pretty`。
`spec` 的 `quad` 也只在節點真的有旋轉或 skew 時才輸出（正矩形的四個角就是 `screenRect`，
寫出來只是重複）；實測 130 個節點裡 79 個可以省掉，且沒有任何一個該寫的被漏掉。

預設輸出到 `out/`，檔名會附上一段參數雜湊，因此不同選項不會覆蓋彼此；
寫檔皆為原子操作。多個 agent 並行時可用 `CSDVIEW_OUT` 環境變數各自指定輸出根目錄，
用 `CSDVIEW_PROJECTS` 各自指定專案登錄檔。

| 指令 | 用途 |
| --- | --- |
| `info` | 檔案結構、動畫清單與長度、節點統計、引用的 `.csd` |
| `render` | 渲染單一影格成 PNG |
| `sheet` | 把一段動畫平鋪成一張總覽圖，一眼看完整段動作 |
| `frames` | 批次輸出每一格 PNG |
| `gif` | 輸出動畫 GIF |
| `tree` | 節點階層與每個節點在畫面上的位置 |
| `find` | 依名稱／類型搜尋節點並回報畫面座標 |
| `spec` | 匯出節點規格 JSON，供移植後做結構化比對 |
| `compare` | 與參考圖（Cocos Creator 截圖）比對差異 |
| `doctor` | 檢查缺少的資源、過期版面、文字爆框、貼圖沒指向圖集、元件尺寸為 0 |
| `list` | 列出資料夾底下所有 `.csd` |

```bash
csdview info Deal.csd
csdview render Deal.csd --anim deal --frame 30 -o out/deal30.png
csdview render Deal.csd --annotate --json        # 圖 + 每個節點的畫面座標
csdview sheet Deal.csd --anim deal --count 12
csdview compare Deal.csd creator_screenshot.png --anim deal --frame 30
csdview render Deal.csd --no-full-bounds       # 只畫設計區（預設會連畫布外一起畫）
```

詳細用法與比對流程見 [AGENT_GUIDE.md](AGENT_GUIDE.md)。

想讓某個專案裡的 agent 自動知道這個工具，把 [dropin/CLAUDE.md](dropin/CLAUDE.md)
複製到該專案根目錄即可，見 [dropin/README.md](dropin/README.md)。

## 當作 Python 函式庫

```python
from csdview import render_file

doc, result = render_file("Deal.csd", frame=30, animation="deal")
result.image.save("frame30.png")

for node in result.nodes:
    print(node.path, node.rect, node.source)
```

## 支援的內容

| 項目 | 狀態 |
| --- | --- |
| 檔案格式 | Cocos Studio 2.x（`<GameProjectFile>`）與 3.x（`<GameFile>`） |
| 節點 | Sprite、ImageView、Panel、Button、CheckBox、Text、TextBMFont、Node、Layer、ProjectNode、ScrollView／ListView／PageView、LoadingBar、Slider |
| 變換 | 位移、旋轉、skew（RotationSkewX/Y 分開）、縮放、錨點、翻轉 |
| 外觀 | 透明度與顏色的階層累乘、加法混色、九宮格、`ClipAble` 裁切、Panel 底色 |
| 動畫 | Position／Scale／RotationSkew／Alpha／CColor／VisibleForFrame／FileData 換圖／BlendFunc／AnchorPoint／FrameEvent／巢狀動畫 ActionValue |
| 緩動 | cocos2d-x 全部 31 種 tweenfunc，以及 `<Points>` 自訂貝茲曲線 |
| 資源 | 鬆散 PNG、`.plist` 圖集（含旋轉與裁邊還原）、AngelCode `.fnt` 點陣字、TTF／系統字型（含中文） |
| 巢狀 | `ProjectNodeObjectData` 遞迴載入，含循環偵測與 `InnerActionFrame` 的 Loop／NoLoop／SingleFrame |

粒子系統只畫出洋紅色標示框標出位置與範圍，不做實際模擬（可用 `--no-particles` 關掉）。

## 已知限制

- 文字用系統字型渲染，字寬會跟 Cocos Studio 量到的設計值有些微差異，字距不會與引擎逐像素相同。
  `IsCustomSize="True"` 的文字會依框寬自動斷行（中日文任意斷、西文退回單字邊界），
  行距等於字級——這是拿 1161 個單行文字節點比對 Cocos Studio 的設計高度定出來的。
  行數超過框高時不裁切照樣畫出來（cocos 的 Label 預設 `Overflow::NONE` 也是如此），
  但 `doctor` 會把這些節點列在 `textOverflow`。
  位置本身是對齊的：自動尺寸的文字節點（沒有 `IsCustomSize`）改用自己量到的尺寸套錨點，
  中心會落在設計位置上；`IsCustomSize="True"` 的固定框則依對齊方式擺放、溢出不裁切。
- `Type="Default"` 的資源是 Cocos Studio 內建佔位圖，這裡用灰色方塊代替，不算缺資源。
- 圖被 `.csi` 打包進 plist 圖集之後，`.csd` 必須寫 `Type="MarkedSubImage"` 並指定 `Plist`。
  寫成 `Type="Normal"` 時 csdview 照樣讀得到磁碟上的散圖、畫面完全正常，
  Cocos Studio 卻是照資源樹解析，開起來會整片掉圖。`doctor` 的 `atlasMismatch`
  就是為了驗這種檔——它讀原始 XML，所以連渲染用不到的 `PressedFileData`／
  `DisabledFileData` 也一起檢查。
- Button 這類靠 `Size` 排版的元件，尺寸 0×0 時 csdview 照貼圖原始尺寸畫得出來，
  Cocos Studio 卻完全不顯示。`doctor` 的 `zeroSize` 會抓出來。只認「兩邊都是 0」：
  單邊 0 是進度條填充的正常寫法（寬度由程式在執行時撐開）。
- ScrollView／ListView 只畫出內容並依 `ClipAble` 裁切，不模擬捲動位置與自動排版。
- 粒子系統不做模擬。

## 並行與執行緒安全

批次指令（`frames`／`gif`／`sheet`）會用執行緒池平行渲染，網頁伺服器也會跨請求共用
同一個 `Renderer`。因此 `Renderer` 與 `AssetLibrary` 的所有快取都必須是執行緒安全的。

改動這兩個類別時請遵守 [csdview/cache.py](csdview/cache.py) 裡的規則：

- 快取一律走 `cache.memoize()`，不要自己寫「查了沒有就算一份存進去」
- 快取鍵必須穩定（用資源路徑），**絕對不要用 `id()`**——物件回收後 id 會被重複使用
- 快取回傳的是共用物件，建立後就不能再修改

改完跑一次競態測試（離開碼 0 代表沒問題）：

```bash
python tests/test_concurrency.py <cocosstudio 資料夾>
```

## 測試

```bash
python tests/test_agent_flow.py <cocosstudio 資料夾>   # agent 流程能否串接
python tests/test_concurrency.py <cocosstudio 資料夾>  # 多執行緒是否有競態
python tests/test_interop.py <某個 .csd>              # 幾個工具同時動作會不會互相弄壞
python tests/test_editor_checks.py                    # 建出來的檔 Cocos Studio 打不打得開
python tests/test_multi_agent.py <某個專案的 .ccs>     # 多個 agent 同時用會不會互相弄壞
python tests/test_ui_picker.py                        # 網頁介面的專案選單
node   tests/test_watch.js                            # 網頁的自動更新行為
```

`test_interop.py` 測的是「同一批 `.csd` 被好幾個工具同時碰」的情況：MCP 編輯器
（可能有好幾個行程）、csdview CLI（多個 agent 各自一個行程）、csdview serve，
偶爾還有 Cocos Studio。涵蓋解析途中檔案被換掉時文件快取要自我修正、影格快取的
兩道上限、兩個編輯器同時存檔只能有一個贏、讀檔期間的原子寫入不會讓內容壞掉，
以及多個 agent 的輸出不會互相蓋掉。需要 `csdmcp` 的項目在找不到它時會跳過。

`test_watch.js` 把 `index.html` 裡的 `<script>` 抽出來，用假的 DOM、假的 fetch 與
可控的時鐘跑一次，所以測到的是產品裡真正那份 JS。它涵蓋連續寫檔期間不重載、
寫完只重載一次、寫個不停也不會無限等、關掉開關與分頁在背景時不發請求。

`test_agent_flow.py` 完整走一次文件描述的移植驗證流程，每一步只用前一步 JSON 裡
承諾的欄位，確保 agent 照文件寫的程式真的跑得通——包含 `designRect` 為 `null`
（約六成的檔案）這種容易被漏掉的情況。

## 資源根目錄怎麼找

`.csd` 裡的 `FileData.Path` 是相對於專案的 `cocosstudio` 資料夾。工具會從 `.csd` 往上找：
名為 `cocosstudio` 的祖先資料夾 → 含 `.ccs` 專案檔的資料夾 → 檔案自身目錄。
找不到時可以用 `--root` 指定，或用 `--extra-root` 追加搜尋目錄（可重複）。
路徑對不上時還會退而用檔名在整個專案裡搜尋，所以搬過資料夾的專案通常也能正常顯示。
