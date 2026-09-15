# ocw-to-study

把**上課錄影 mp4** 轉成一份 Obsidian 相容的「逐頁講解」Markdown 筆記：
每一張投影片配上該段老師的逐字稿（右側固定高度捲動框）與重點拆解（下方全寬）。

這是一個 [Claude Code](https://claude.com/claude-code) skill。完整規格、每一步的判準與踩過的坑都在 [`SKILL.md`](SKILL.md)。

## 它做什麼

| 步驟 | 指令 | 產出 |
|---|---|---|
| 抽投影片 | `vts.py extract` | 從影片畫面原生解析度擷取，多幀中位數去除雷射筆光點 |
| 檢查漏頁 | `vts.py coverage` | 與講義 PDF 逐頁比對 |
| 轉逐字稿 | `vts.py asr` | faster-whisper（有 GPU 用 large-v2），5 分鐘分段可續跑 |
| 校正專有名詞 | `vts.py fix` | 套用共用校正表 `corrections_common.json` ＋ 各章專屬表 |
| 產出筆記 | `vts.py build` | 依人工審定的段落斷點組出 `.md` |
| 渲染預覽 | `vts.py preview` | headless Chrome 截成 PNG |

段落斷點（`breaks.json`）與重點拆解（`content.json`）需要通讀逐字稿後人工產出，這一步刻意不自動化，原因見 `SKILL.md` 步驟 5。

## 安裝

把整個資料夾放到 Claude Code 的 skills 目錄：

```
~/.claude/skills/ocw-to-study/
├── SKILL.md
└── scripts/
    ├── vts.py                    主程式（extract / coverage / asr / fix / build / preview）
    ├── note_utils.py             段落合併的機械規則
    ├── tex.py                    句中符號轉 LaTeX（V_GS → $V_{GS}$）
    ├── formula.py                整行算式轉 LaTeX
    └── corrections_common.json   跨課程共用的 ASR 誤聽校正表
```

之後在 Claude Code 說 `/ocw-to-study`，或提供上課錄影並要求做逐頁筆記。

## 依賴

- Python 3.10+
- `ffmpeg`、`ffprobe`（需在 PATH）
- `pip install faster-whisper zhconv Pillow numpy markdown PyMuPDF`
- Google Chrome 或 Edge（`preview` 用）
- 選用：NVIDIA GPU＋CUDA（轉錄快約 5 倍）

在 Windows 11 上開發與測試。

## 單獨使用腳本

```
python scripts/vts.py asr      --root <課程資料夾> --ch 3 --video "<mp4>"
python scripts/vts.py extract  --root <課程資料夾> --ch 3 --video "<mp4>"
python scripts/vts.py coverage --root <課程資料夾> --ch 3 --pdf "<講義.pdf>"
python scripts/vts.py fix      --root <課程資料夾> --ch 3
python scripts/vts.py build    --root <課程資料夾> --ch 3
python scripts/vts.py preview  --root <課程資料夾> --ch 3 --batch 5
```

副產物寫進 `<課程資料夾>/AI/CH<N>/`，筆記寫進 `<課程資料夾>/`。
