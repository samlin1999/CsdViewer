# 移植驗證：對照原始 Cocos Studio 畫面

這個專案的介面從 Cocos Studio 移植而來。原始的 `.csd` 檔案可以用 `csdview` 渲染成圖片，
用來確認移植後的畫面與原始設計一致。

## 規則（動手前先讀）

1. **圖產生後必須用 Read 讀進來。** `render`／`sheet`／`compare` 只是把 PNG 寫成檔案。
   只回報「已輸出 out/xxx.png」不算完成任務——你要真的看過那張圖。
2. **先分清楚差異是「工具的已知限制」還是「移植做錯」**，判準見下一節。
   把已知限制當成 bug 回報，比漏報還糟：會讓人去改本來正確的版面。
3. **畫面缺東西或版面怪怪的先跑 `doctor`。** 它會一次檢查五件事：缺失資源、
   `staleLayout`（沒更新的舊資源）、`textOverflow`（文案太長塞不進固定框）、
   `atlasMismatch`（貼圖該指向圖集卻寫成獨立檔）、`zeroSize`（元件尺寸是 0）。
   前三者不是移植做錯，是來源資料本身的問題；後兩者是**建檔時就寫錯**——
   在這裡渲染完全正常、用 Cocos Studio 開卻掉圖或整個元件不見，
   只有這兩個檢查驗得出來。
4. **`referenceAspectDelta > 0.02` 時先修截圖，不要動版面。** 那代表兩邊取景範圍不同。
5. **找不到 `csdview` 指令**就請使用者到 csdview 專案資料夾執行一次 `pip install -e .`。
   不要自行猜測工具的安裝路徑。

## 判斷準則

### 這個工具在哪些事情上可以逐像素相信

| 可以相信（差異即是問題） | 只能參考（差異屬正常） |
| --- | --- |
| 固定框文字的自動斷行位置 | 文字若寫成字面的 `
`（而非真換行）會照字面畫出來 |
| 節點的位置與尺寸（`screenRect`） | 純英數字的字寬——實測比設計值**寬約 24%** |
| 中文字寬——實測中位數誤差 **0%** | 字距與字型輪廓（用的是系統字型） |
| 圖片內容、九宮格拉伸、加法混色 | 粒子——只畫洋紅色空框，不做模擬 |
| 透明度與顏色的階層累乘 | ScrollView／ListView 的捲動位置與自動排版 |
| 動畫時間軸的求值（含緩動曲線） | `gif` 的播放速度（GIF 延遲單位是 1/100 秒） |

推論方式：**位置對不上一定要查；字寬對不上先看是不是純英數。**

### 常見誤判

| 看到的現象 | 不要直接下的結論 | 先做這件事 |
| --- | --- | --- |
| `diffPixelRatio` 很高 | 「版面做錯了」 | 看 `shiftToAlignReference`：整體位移是最常見的移植錯，改錨點就好 |
| 文字區域有差異 | 「文字位置錯了」 | 中文字寬誤差 0%，純英數偏寬 24%。**只有整段文字位移才是問題** |
| 畫面歪掉／不對稱 | 「要調 Creator 的版面」 | 跑 `doctor` 看 `staleLayout`；也可能是 `.csd` 本身沒更新 |
| 文字撐破框、蓋到其他元件 | 「渲染錯了」 | 跑 `doctor` 看 `textOverflow`：文案太長塞不進設計好的框，引擎端同樣會爆版 |
| 渲染出來整片空白 | 「渲染器壞了」 | 跑 `doctor` 看缺失資源；或節點被隱藏／縮放為 0 |
| 有洋紅色空框 | 「多畫了東西」 | 那是粒子標示框，比對時加 `--no-particles` |
| 有灰色對角線方塊 | 「圖片缺失」 | 那是 Cocos Studio 內建佔位圖，本來就沒有實際資源 |
| `designRect` 取值出錯 | 「工具回傳格式不對」 | 約六成檔案沒有固定畫布，`designRect` 是 `null`，取值前要判斷 |
| 尺寸與截圖不同 | 「渲染尺寸錯了」 | 看 `clamped`：內容過大會自動降尺度；或本來就沒有固定畫布 |

## 指令

```
csdview <指令> <檔案.csd> [選項]
```

任何工作目錄都能呼叫。圖片預設寫到當下目錄的 `out/`，用 `-o` 指定路徑。

**多個 agent 同時使用時不會互相干擾**：預設檔名會帶上一段由參數算出的雜湊，
所以不同選項一定是不同檔案；寫檔是先寫暫存檔再改名（原子操作），
不會讀到別人寫到一半的圖。要完全隔離就設環境變數 `CSDVIEW_OUT=<你的目錄>`。
**永遠以 JSON 回報的 `image` 路徑為準**，不要自己拼檔名。
所有指令都支援 `--json`：stdout 是乾淨的 JSON，進度訊息走 stderr
（**看到進度輸出代表還在跑，不是卡住**）。

| 指令 | 用途 |
| --- | --- |
| `info` | 動畫清單與長度、設計解析度、節點統計、引用的其他 `.csd` |
| `render` | 渲染單一影格 |
| `sheet` | 整段動畫平鋪成一張，最快看懂動作 |
| `tree` | 節點階層與每個節點在畫面上的位置 |
| `find` | 依名稱或型別搜尋節點，回報畫面座標 |
| `spec` | 每個節點的座標、尺寸、貼圖路徑、文字內容 |
| `compare` | 與 Creator 截圖比對，指出哪個節點畫錯 |
| `doctor` | 檢查資源缺失與版面過期的舊資源 |
| `frames` | 批次輸出每一格 PNG |
| `list` | 列出資料夾底下所有 `.csd` |

```bash
csdview info Deal.csd --json
csdview sheet Deal.csd --anim deal --count 12 -o out/s.png
csdview render Deal.csd --anim deal --frame 30 --annotate -o out/f30.png
csdview find Deal.csd --type Button --json
csdview compare Deal.csd creator.png --anim deal --frame 30 --json
```

`--annotate` 會在圖上疊出節點外框與名稱，方便把看到的東西對應到節點名。

### 先問對問題，再問整份

`tree` 與 `spec` 整份倒出來很貴（130 個節點約 8000 token，是一張渲染圖的六倍）。
知道要找什麼就別要整份：

| 想知道 | 用這個 | 大約 |
| --- | --- | --- |
| 版面大致長怎樣 | `tree --depth 2` | 2600 |
| 某個節點在哪、多大 | `find --name <名稱>` | 300 |
| 某一塊的完整規格 | `spec --node <路徑>` | 600 |
| 整份規格 | `spec` | 8800 |

`--json` 預設是緊湊輸出（要給人看再加 `--pretty`）。

`--hide <節點路徑>` 強制隱藏某節點（含子節點），`--show <節點路徑>` 強制顯示——
連 `.csd` 裡設為隱藏的節點也能叫出來。兩者都可重複指定。
用途：擋住前景看背後疊了什麼、或單獨檢視被藏起來的面板。

### 影格編號

指定 `--anim` 時，`--frame` 是「**該段動畫的第幾格**」。JSON 會同時回報絕對影格 `frame`
與相對影格 `animationFrame`。時間軸固定 60fps，也可以改用 `--time <秒>`。

### 取景範圍

不少 `.csd` 把元件擺在設計區之外，所以**預設會畫出完整內容**，原設計區以虛線標出。

- `designRect` 回報設計區在圖上的位置 `[x, y, w, h]`
- **`designRect` 可能是 `null`**：約六成的 `.csd` 沒有固定畫布（`info` 的 `canvasSize`
  是 `[0, 0]`），這種檔案沒有「設計解析度」可言。取值前一定要先判斷。
- `screenRect` 的原點是**完整畫布的左上角**，不一定是設計區左上角
- `designRect` 不是 `null` 時，`screenRect` 減去它的前兩個值就是設計解析度座標
- 只想看設計區：加 `--no-full-bounds`（沒有固定畫布的檔案不受影響）

`compare` 也預設完整內容，所以 Creator 端截圖**涵蓋所有可視內容就好**，不必配合設計解析度。
它一定不畫虛線框（那會被算成差異）。

**拿 `render` 的輸出當基準圖時要加 `--bg none --no-guide`。** `render` 預設背景是 checker
格子，`compare` 預設在純黑上比——畫面沒鋪滿畫布時（局部面板、或元件被移開）
會得到一個假的大差異。忘了加也不會被誤導：`compare` 會告訴你「形狀都吻合但顏色到處不同」。

### 座標系統

| 欄位 | 座標系 |
| --- | --- |
| `screenRect` | 畫布像素，原點左上、Y 軸向下——與截圖一致，**比對位置用這個** |
| `position` | Cocos 節點座標，原點左下、Y 軸向上——與 `.csd` 原始值一致 |

## 移植驗證流程

1. `info` — 有哪幾段動畫、各自的影格範圍、引用了哪些巢狀 `.csd`
2. `doctor` — 資源是否齊全、有無 `staleLayout`
3. `render` 或 `sheet` 產生基準圖，**Read 來看**
4. Creator 端截出對應影格：涵蓋所有可視內容即可，背景與 `--diff-bg` 一致（預設純黑）
5. `compare` 比對
6. 依 `verdict` 修正，重跑到 `match`

## 判讀 compare

**先讀 `hints[]`。** 工具會依實際數字算出判讀建議，順序即重要性，而且會在前提失效時
（例如取景範圍不一致、有資源缺失）直接告訴你其他數字不可信、不要據此修改版面。
它涵蓋了下面所有規則，照著做就不會漏掉前提。

`hints` 是結論，下面的欄位是依據——需要自己推導或要回報細節時再看。

`verdict` 是工具依實際門檻算出來的，不是主觀描述：

| `verdict` | 判定條件 | 下一步 |
| --- | --- | --- |
| `match` | `diffPixelRatio < 0.01` 且 `meanDiff < 0.01` | 通過 |
| `shifted` | 位移信心 > 20，且扣掉位移後 `diffPixelRatio < 0.02` | 內容都對，只差整體位移。看 `shiftToAlignReference`，多是 Creator 的錨點、Canvas 對齊或設計解析度設定不同 |
| `close` | `diffPixelRatio < 0.08` | 看 `worstRegions[]`，確認是不是只有文字造成的 |
| `different` | `diffPixelRatio >= 0.08` | 看 `afterShiftCompensation.worstRegions[]` 前幾名的 `path` |

| 欄位 | 意義與判準 |
| --- | --- |
| `diffPixelRatio` | 差異像素佔比。單一像素要 RGB 任一通道差超過 5% 才算 |
| `shiftToAlignReference` | 參考圖要平移多少 px 才對齊。**整張圖差一個偏移是最常見的移植錯** |
| `afterShiftCompensation` | 扣掉整體位移後重算的差異，**這才是真正畫錯的部分** |
| `alphaIoU` | 只看形狀吻合度，與顏色無關。接近 1 表示東西都在、只是顏色不同 |
| `referenceAspectDelta` | 長寬比落差。**超過 0.02 就代表取景範圍不一致**，此時差異數字不可信 |
| `referenceResized` | 截圖被縮放對齊過，這是正常的 |
| `worstRegions[]` | 只列葉節點、面積 64px 以上，最多 15 個，依差異比例排序 |
| `worstRegions[].kind` | 這個差異該怎麼看：`content` 真的要查、`text` 字型差異、`particle` 標示框、`placeholder` 內建佔位圖 |

背景不是黑色時用 `--diff-bg <色碼>` 指定成與截圖相同的顏色。

離開碼：`match`／`shifted`／`close` → `0`，`different` → `1`，執行錯誤 → `2`。

### 判讀範例

```json
{ "verdict": "different", "diffPixelRatio": 0.41,
  "shiftToAlignReference": [0, -18], "shiftConfidence": 412,
  "afterShiftCompensation": { "diffPixelRatio": 0.03,
    "worstRegions": [ { "path": "Layer/panel/txt_title", "diffPixelRatio": 0.62 },
                      { "path": "Layer/panel/btn_ok", "diffPixelRatio": 0.55 } ] } }
```

正確的推論順序：

1. `diffPixelRatio` 0.41 很高，但**先別下結論**。
2. `shiftConfidence` 412 遠高於 20，`shiftToAlignReference` 是 `[0, -18]`
   → 整張圖在 Y 軸差 18px。這是錨點或對齊設定問題，不是每個元件都擺錯。
3. 扣掉位移後只剩 0.03 → **絕大部分差異都是那個位移造成的**。
4. 剩下的 `worstRegions`：`txt_title` 是文字節點，先確認是不是純英數字寬差異；
   `btn_ok` 不是文字，這個要查——用 `find` 或 `spec` 取它的 `screenRect` 對照 Creator。
5. 回報時要分開講：一個整體位移（改一處即可）＋ 一個節點的實際問題。

反例：直接說「41% 的畫面都不對，版面要重做」——這會讓人白做很多工。

## 症狀對照

| 症狀 | 先查 | 通常原因 |
| --- | --- | --- |
| 渲染出來整片空白 | `doctor`、`tree` | 資源缺失，或節點全被隱藏／縮放為 0 |
| csdview 正常但 Cocos Studio 掉圖 | `doctor` 的 `atlasMismatch` | 圖被打包進 plist 圖集，`.csd` 卻寫成 `Type="Normal"` |
| csdview 正常但 Cocos Studio 少了某個元件 | `doctor` 的 `zeroSize` | Button 這類靠 Size 排版的元件尺寸是 0×0 |
| 差異比例很高但畫面看起來一樣 | `shiftToAlignReference` | 整體位移，調 Creator 的錨點或對齊方式 |
| 差異集中在文字 | 判斷準則那一節 | 純英數偏寬 24% 屬正常；整段位移才是問題 |
| `referenceAspectDelta > 0.02` | 截圖範圍 | 兩邊取景不同，先修截圖 |
| 畫面出現洋紅色空框 | — | 粒子標示框。比對時加 `--no-particles` |
| 畫面出現灰色對角線方塊 | — | Cocos Studio 內建佔位圖，原本就沒有實際資源 |
| JSON 的 `clamped` 是 `true` | `--max-pixels` | 內容過大已自動降尺度，輸出尺寸與設計解析度不同 |
| 指令跑很久沒結束 | stderr 的進度 | 批次指令本來就慢，有進度輸出就是正常的。`frames`／`gif`／`sheet` 已自動並行，`--jobs N` 可調 |

## 其他限制

- 粒子只畫標示框，不做實際模擬
- ScrollView／ListView 只畫內容並依 `ClipAble` 裁切，不模擬捲動位置與自動排版
- `gif` 的播放速度是近似值；**要精確逐格比對請用 `frames` 或 `sheet`**
- 找不到資源根目錄時用 `--root <cocosstudio 資料夾>`，或 `--extra-root` 追加搜尋目錄
- 要讓純英數字寬也對齊，可用 `--font ARIALN.TTF`（實測誤差可降到 5% 內）

更詳細的說明見 csdview 專案裡的 `AGENT_GUIDE.md`。
