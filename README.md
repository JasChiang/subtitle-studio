# Subtitle Studio

本機 web GUI：**Apple 裝置端 ASR 取得字級時間碼 → Gemini 看影片做獨立參考稿 → 校正文字 → 斷句 → 翻譯**，可在瀏覽器裡對著影片逐條修字幕、匯出 SRT。
做法沿用 caption-lab：參考稿不看 ASR 文字（獨立第二意見），時間碼一律來自 Apple 的字級時鐘，Gemini 只改文字。

## 用法
```bash
./setup.sh          # 編譯 bin/apple-asr（macOS 26+、swiftc、ffmpeg），建立 .env
# 編輯 .env：GEMINI_API_KEY=...
./run.sh            # 開 http://127.0.0.1:8765
```
GUI：「＋ 新增影片」→ 選檔（直接讀原檔，不複製）/ 拖曳上傳 → 填語言、影片脈絡、詞彙表 → 建立後自動跑完整流程。

不開 GUI：`python3 -m studio.cli <video> --opt locale=en-US --opt 'context=…'`，或 `--project <id> --stages cues,translate` 重跑部分步驟。

## 步驟（每步都有快取，可單獨重跑）
| 步驟 | 內容 | 產物 |
|---|---|---|
| asr | ffmpeg 抽 16k 音軌 → SpeechTranscriber（字級 audioTimeRange） | `asr.json` |
| reference | 依「每段分鐘數」切 360p/2fps 代理檔上傳 File API，Gemini 逐字聽寫＋講者＋專有名詞 | `reference/chunk_*.json`, `reference.json` |
| correct | 把 ASR 段落＋同時段參考稿交給 Gemini（文字）校正；段內換講者標 `||` | `corrected.json` |
| cues | difflib 把校正文字對齊回 ASR 字級時間（相同／一對一替換沿用原時間，新增字在空隙內分配），再依標點、停頓、長度斷句 | `cues.json` |
| translate | 每 60 條一批翻譯（含前後文），預設台灣繁中 | `cues.json` 的 `tr` |

專案資料在 `projects/<id>/`。表格內手動修改存回 `cues.json`；重跑「斷句」會覆蓋手改（原文相同的譯文會沿用）。

## 編輯器快速鍵
⌥Space 播放／暫停 · ⌥[ / ⌥] 把目前字幕的入點／出點設成播放點 · ⌥S 在播放點切開 · ⌥← ⌥→ 跳 2 秒 · ⌘S 存檔。
點 # 欄跳到該條；✎ 標記代表校正後與 ASR 不同（滑過可看 ASR 原文）。

## 匯出
雙語 SRT（原文在上／譯文在上）、原文 SRT、譯文 SRT、VTT、字級 JSON。
