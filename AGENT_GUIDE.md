# csdview — AI agent 使用指南

這份文件是寫給會呼叫這個工具的 AI agent。主要情境：**把 Cocos Studio 的 `.csd` 移植到
Cocos Creator 之後，驗證畫面是否正確。**

所有指令都支援 `--json`。圖片一律寫成檔案，JSON 會回報絕對路徑，讀 JSON 就知道要去看哪張圖。

**多個 agent 並行時的輸出隔離**：預設檔名會附上一段參數雜湊（例如
`Deal_deal_f30_a3f21c8e.png`），所以只要選項不同就一定是不同檔案，不會互相覆蓋；
相同指令則得到相同路徑，可重複執行也不會累積垃圾。所有寫檔都是先寫暫存檔再改名，
別的行程不會讀到寫到一半的內容。需要完全隔離時設 `CSDVIEW_OUT=<目錄>`。
**請一律使用 JSON 回報的 `image` 路徑，不要自己組檔名。**

---

## 1. 先看懂這個檔案有什麼

```bash
csdview info <檔案.csd> --json
```

重點欄位：

- `animations[]` — 每段動畫的 `name`、`start`、`end`、`frames`、`seconds`。
  **移植時要逐段確認的清單就是這個。**
- `duration` — 整條時間軸長度（影格；固定 60fps）
- `canvasSize` — 設計解析度。`[0, 0]` 代表這個檔案沒有固定畫布，渲染時會依內容自動裁切
- `references[]` — 這個檔案引用了哪些別的 `.csd`（巢狀預製件，移植時要一起處理）
- `nodeTypes` — 用到哪些節點類型

## 2. 看畫面

```bash
# 單一影格
csdview render <檔案.csd> --anim <動畫名> --frame <N> -o out/a.png --json

# 整段動畫攤成一張圖（判斷動作對不對最快的方式）
csdview sheet <檔案.csd> --anim <動畫名> --count 12 --cols 4 -o out/sheet.png
```

`--frame` 在有指定 `--anim` 時是「**該段動畫的第幾格**」，JSON 會同時回報絕對影格 `frame`
與相對影格 `animationFrame`。也可以改用 `--time <秒>`。

想知道畫面上哪一塊是哪個節點，加 `--annotate`：會疊上每個節點的外框與名稱。

要單獨檢視某個節點時：

```bash
csdview render <檔案.csd> --hide 節點路徑 -o out/a.png   # 擋住它，看背後疊了什麼
csdview render <檔案.csd> --show 節點路徑 -o out/b.png   # 叫出 .csd 裡設為隱藏的節點
```

兩者都可重複指定，`--show` 會穿透父層的隱藏狀態。節點路徑用 `tree` 或 `find` 取得。

**畫布外的內容**：不少 `.csd` 會把元件擺在設計區之外（作者先放著、之後在程式裡定位）。
`render`／`sheet`／`tree`／`find`／`spec` **預設就會畫出完整內容**，原設計區以虛線標出，
JSON 的 `designRect` 回報它在圖上的位置。想只看設計區就加 `--no-full-bounds`。

因此 `screenRect` 的原點是**撐大後的畫布左上角**，不一定等於設計區左上角。
`designRect` 不是 `null` 時，把 `screenRect` 減去它的前兩個值就是設計解析度座標。

**但 `designRect` 經常是 `null`**：約六成的 `.csd` 沒有固定畫布（`info` 的 `canvasSize`
為 `[0, 0]`），畫布尺寸完全由內容決定。這種檔案沒有「設計解析度」這回事，
不需要也無法換算——`screenRect` 就是唯一的座標系。**取值前務必先判斷是否為 `null`。**

`compare` 同樣預設畫完整內容，這樣才對得上「截取所有可視內容」的截圖；
但它一定不畫設計區虛線框，避免標註被當成差異。

## 3. 拿到節點的精確座標

```bash
csdview spec <檔案.csd> --frame <N> -o out/spec.json
```

輸出每個節點的 `screenRect`（畫布像素座標，**原點左上、Y 軸向下**，與截圖一致）、
`size`、`position`（Cocos 節點座標，原點左下、Y 軸向上）、`source`（貼圖路徑）、
`text`、`alpha`、`visible`。

移植後想確認某個元件擺對位置，比對 `screenRect` 最直接。

搜尋特定節點：

```bash
csdview find <檔案.csd> --name btn --json
csdview find <檔案.csd> --type Button --json
```

## 4. 與 Cocos Creator 的畫面比對 ← 主要用途

```bash
csdview compare <檔案.csd> <creator截圖.png> \
    --anim <動畫名> --frame <N> -o out/diff.png --json
```

**截圖要求**：

- Creator 端截出**所有可視內容**即可，不必配合設計解析度——`compare` 預設也是渲染完整內容
  （`--full-bounds`），兩邊的取景範圍因此一致。
- 尺寸不必完全相同，工具會自動縮放參考圖並在 `referenceResized` 標記。
  但要留意 `referenceAspectDelta`：**長寬比落差超過幾個百分點就代表取景範圍不一致**，
  縮放會扭曲畫面、差異數字會偏高，這時要先確認截圖範圍而不是急著改版面。
- 背景要一致。Creator 端不是純黑時，用 `--diff-bg <色碼>` 指定成相同顏色。
- 自己用 `render` 產生基準圖再拿去比對時，記得加 `--no-guide`——設計區虛線框是給人看的標註，
  會被算成像素差異。`compare` 內部已自動關閉它。

輸出重點：

**`hints[]` 先讀。** 工具依實際數字算出的判讀建議，順序即重要性。前提失效時
（長寬比不符、有缺資源）它會直接說「其餘數字不可信」並停止，避免你依據壞掉的數字下判斷。
下面的欄位是它的依據，需要細節時再看。

| 欄位 | 意義 |
| --- | --- |
| `hints[]` | **判讀建議，最先看這個** |
| `verdict` | `match`／`shifted`／`close`／`different` |
| `worstRegions[].kind` | `content` 真的要查／`text` 字型差異／`particle` 標示框／`placeholder` 內建佔位圖 |
| `diffPixelRatio` | 差異像素佔比（0~1），最直觀的指標 |
| `shiftToAlignReference` | **參考圖要平移多少 px 才能對齊渲染圖**。整張圖差一個偏移是移植最常見的錯 |
| `afterShiftCompensation` | 扣掉整體位移後重算的差異。**這才是真正畫錯的部分** |
| `afterShiftCompensation.worstRegions[]` | 扣掉位移後差異最大的節點，附 `path` 與 `screenRect` |
| `alphaIoU` | 只看「有沒有畫東西」的形狀吻合度，與顏色無關 |

判讀方式：

- `verdict: "match"` → 畫面一致，通過。
- `verdict: "shifted"` → 內容都對，只差整體位移。看 `shiftToAlignReference`，
  通常是 Creator 端的錨點、Canvas 對齊方式或設計解析度設定不同。
- `verdict: "different"` → 看 `afterShiftCompensation.worstRegions[]` 的前幾名，
  那些 `path` 就是畫錯的節點。再用 `find` 或 `spec` 查那個節點的正確規格。

`diff.png` 是三格對照圖（渲染／參考／差異熱區），可以直接看。

離開碼：一致或接近回傳 `0`，`different` 回傳 `1`，方便接進 CI。

## 5. 資源缺失

```bash
csdview doctor <檔案.csd> --json
```

`missingAssets[]` 列出找不到的檔案。畫面缺東西時**先跑這個**——很可能是資源沒搬過去，
而不是渲染錯誤。找不到根目錄時用 `--root <cocosstudio 資料夾>` 指定。

`textOverflow[]` 列出文字撐破固定框的節點（附框的尺寸與實際需要的尺寸）。
cocos 的 Label 預設不裁切，所以引擎端也會爆版——這是來源文案太長，不是移植做錯。

`staleLayout[]` 列出百分比版面與絕對值對不上的節點：Cocos Studio 會同時存
`PreSize`／`PrePosition`（百分比）與 `Size`／`Position`（絕對值），兩者對不上就代表
父層尺寸改過而該節點沒重算。**這是「沒更新的舊資源」的指紋**——畫面看起來歪掉時先查這個，
多半不是移植做錯。全語料庫 1720 個檔案中只有 12 個節點有這種情形。

## 6. 批次處理

```bash
csdview list <資料夾> --detail --json     # 列出所有 .csd 與其動畫
csdview frames <檔案.csd> --anim <動畫名> --out out/frames --step 2
csdview gif <檔案.csd> --anim <動畫名> --step 2
```

`frames`／`gif`／`sheet` 執行中會把進度寫到 stderr（stdout 仍是乾淨的 JSON），
所以看到進度輸出代表還在跑，不是卡住。

這三個指令會自動並行渲染（實測 `frames` 60 格快 2.85 倍），並行度依畫布大小自動決定，
大畫布會自動降回單執行緒以免吃爆記憶體。要手動指定用 `--jobs N`（`--jobs 1` 即序列）。

`gif` 的播放速度只是近似值：GIF 的每格延遲單位是 1/100 秒，60fps 換算後會被量化
（例如 `--step 5` 實際存成 80ms 而非 83ms）。**要精確逐格比對請用 `frames` 或 `sheet`，不要用 GIF。**

---

## 建議的移植驗證流程

1. `list --detail` 取得要處理的 `.csd` 清單與各自的動畫段。
2. 對每個檔案跑 `info`，記下 `animations[]` 與 `references[]`。
3. `doctor` 確認資源齊全。
4. 對每段動畫取幾個代表影格（起、中、末），跑 `render` 或 `sheet` 產生基準圖。
5. 在 Cocos Creator 端截出對應影格。
6. `compare` 逐格比對，依 `verdict` 分流：
   - `shifted` → 調整 Creator 端的錨點／對齊／設計解析度。
   - `different` → 依 `worstRegions[]` 的 `path` 逐一修，再用 `spec` 查正確的座標與貼圖。
7. 重跑 `compare` 直到 `verdict` 為 `match`。

## 注意事項

- 文字字寬的實測誤差（拿 Cocos Studio 的設計值當基準，400+ 個樣本）：
  **中文中位數 0%**（幾乎完全吻合）、**純西文偏寬約 24%**。
  所以中文可以放心比對；**純英數的文字節點本來就會偏寬，不要當成移植錯誤**。
  要讓西文也對齊時用 `--font <字型>` 指定（實測 `ARIALN.TTF` 可把誤差降到 5% 內）。
- 文字的**位置**是對齊的（自動尺寸的文字用自己的尺寸重新套錨點，中心落在設計位置），
  所以某段文字整體明顯位移就是真的有問題，值得回報。
- 粒子只畫洋紅色標示框，不做模擬。比對含粒子的畫面時加 `--no-particles`，
  否則標示框會被算成差異。
- 內容範圍極大的 `.csd`（多半是全螢幕特效）會自動降尺度以免吃光記憶體，JSON 的
  `clamped` 為 `true`、`scale` 小於 1 就代表發生了。此時輸出尺寸與設計解析度不同，
  比對前要把 Creator 截圖縮到同樣大小，或用 `--max-pixels` 調高上限。
- ScrollView／ListView 不模擬捲動位置。
- `Type="Default"` 的灰色方塊是 Cocos Studio 內建佔位圖，原本就沒有實際資源。
