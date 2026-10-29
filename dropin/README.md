# dropin

這個資料夾裡的檔案是**要複製到別的專案**去用的，不是給 csdview 自己用的。

## 前置：先讓 `csdview` 變成可用的指令

在 csdview 專案資料夾執行一次：

```bash
pip install -e .
```

之後在任何目錄都能直接打 `csdview`。這一步是必要的——[CLAUDE.md](CLAUDE.md) 裡刻意不寫
任何絕對路徑，換一台機器或搬動資料夾都不用改檔案。

## CLAUDE.md

最簡單的方式是雙擊 csdview 資料夾裡的 **`設定AI助手.bat`**，把目標專案資料夾
拖進去即可。等效的指令是：

```bash
csdview init <你的Creator專案>
```

`init` 比手動複製好在三點：內容用標記框起來，重跑只會更新那一段；
目標已有 `CLAUDE.md` 時會保留你原本寫的內容；`--name AGENTS.md` 可換成
其他 agent 工具讀的檔名。

之後在那個專案裡工作的 AI agent 每次開始都會讀到它，不必再逐次說明 csdview 的用法。

若該專案已經有 `CLAUDE.md`，就把內容附加到既有檔案後面，不要覆蓋。

其他 agent 工具讀的檔名不同，內容可以直接沿用，複製成對應的檔名即可
（例如 `AGENTS.md`、`.cursorrules`）。
