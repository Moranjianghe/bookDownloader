# hetushu2epub

把 [和圖書 hetushu.com](https://www.hetushu.com) 的線上小說抓下來，打包成 EPUB 3 電子書。

- **零依賴**：只用 Python 3.8+ 標準庫，不需要 `pip install` 任何東西。
- **可續傳**：章節快取在 `cache/<書籍id>/`，中斷後重跑只補缺的章節。
- **會還原段落順序**：本站把章節段落順序打亂，再用一張藏在 HTTP 標頭裡的對照表還原，本工具完整重現了這個演算法。
- **會清掉廣告**：正文裡夾帶的「和圖書」站名／網址標籤（`<bdo>`、`<tt>`、`<s>`… 以及全形字變體）會連內容一起移除，不會留下殘句。

## 快速開始

```bash
# 用書籍 id 或完整網址都可以
python3 hetushu2epub.py 5626
python3 hetushu2epub.py https://www.hetushu.com/book/5626/

# 指定輸出檔名、加快併發
python3 hetushu2epub.py 5626 -o 終極斗羅.epub --workers 6

# 先看看目錄有幾章
python3 hetushu2epub.py 5626 --list

# 只抓某個區間（做小樣本測試很好用）
python3 hetushu2epub.py 5626 --start 1 --end 20 -o 試讀.epub
```

產出後會自動做健檢（zip 結構、`mimetype` 位置、每個 XML 是否合法、章節檔數是否等於章節數）。

## 參數

| 參數 | 說明 | 預設 |
| --- | --- | --- |
| `book` | 書籍 id（如 `5626`）或書目頁網址 | 必填 |
| `-o, --output` | 輸出檔名 | `<書名>.epub` |
| `--start` / `--end` | 章號範圍（1 起算，含頭含尾） | 全部 |
| `--list` | 只列目錄，不抓內文 | 關 |
| `--workers` | 併發執行緒數 | `5` |
| `--delay` | 每次請求的最小間隔（秒） | `0.15` |
| `--cache-dir` | 快取目錄 | `cache/<bid>` |
| `--no-cache` | 不快取、全部重抓 | 關 |
| `--nav-group` | 目錄每幾章分一組（`0` = 不分組） | `100` |
| `--user-agent` | 自訂 UA | Chrome UA |
| `--no-verify` | 跳過產出後健檢 | 關 |

## 抓取原理

1. **書目頁** `/book/<bid>/`：解析書名、作者、簡介、封面與章節清單（`<dl id="dir">` 裡的 `<dd><a>`）。
2. **章節頁** `/book/<bid>/<cid>.html`：正文在 `<div id="content">`，取得 `<h2>` 之後的所有 `<div>` 段落；`<div class="mask">` 是防拷貝遮罩，須排除。
3. **順序對照表** `/book/<bid>/r<cid>.json`：回 `204`，資料在回應標頭 `token` 裡。`base64` 解碼後形如 `34E%24E%5Z%18B%…`，以 `/[A-Z]+%/` 切開得到數字序列，即每個段落的正確位置：

   ```
   序號 < 5  → 佔用該絕對位置，並計入 filled
   序號 ≥ 5  → 放到 (序號 - filled) 的位置
   ```

   這與網站 `section.js` 的 `content.load()` 行為一致。若長度對不上（站方偶發錯誤），工具會退回原順序並在 log 留下提示，絕不丟內容。
4. **去廣告（依前端 CSS 判定，不靠猜）**：站方在 `/command/section.css` 裡用一條規則決定「讀者看不到什麼」：

   ```css
   #content acronym,#content bdo,#content big,#content cite,#content code,
   #content dfn,#content kbd,#content q,#content s,#content samp,
   #content strike,#content tt,#content u,#content var{display:none;}
   ```

   也就是說，這 14 個標籤連內容一律不顯示 —— 它們就是本站的廣告位（`和*图*书`、`hｔtps://ｍ•hｅｔusｈu•ｃoｍ` 之類）。工具直接照抄這份清單，出現即連內容刪除，因此不需要用關鍵字去猜「這段字像不像廣告」，也避開了站方刻意把站名拆成 `和` / `*图*` / `书` 的干擾。另外 `#content small` 只是縮小、`#content ins` 只是排版，屬於可見內容，予以保留；`和图书馆` 這類正常詞也不會被誤刪。

   清理後會再掃一次殘留：`hetushu` / `和图书`（含全形、大小寫、任意分隔符變體）若仍出現，會在 log 警告，`qa_epub.py` 也會獨立複查。
5. **EPUB 3**：`mimetype` 置於首位且不壓縮，`META-INF/container.xml` → `OEBPS/content.opf`；每章一個 XHTML，附 `nav.xhtml`（兩層目錄）、`toc.ncx`（相容舊閱讀器）、封面圖與 CJK 排版用的 CSS。

## 專案結構

```
hetushu2epub.py      # 單檔工具（抓取 / 解碼 / 快取 / EPUB）
cache/<bid>/         # 章節快取（JSON，含標題與已還原的段落）
<書名>.epub          # 產出
```

## 怎麼確認抓對了

手段是「拿瀏覽器當標準答案」：同一頁在瀏覽器裡跑完站方 JS 之後，把 `#content` 中 `display != none` 的元素抓出來，就是讀者真正看到的段落順序。工具輸出要與它逐段一致。

```bash
# 1) 用瀏覽器（Playwright MCP / DevTools console）取得標準答案
#    [...document.querySelector('#content').children]
#      .filter(el => el.tagName === 'DIV' && getComputedStyle(el).display !== 'none')
#      .map(el => el.innerText.trim())
# 2) 與工具輸出比對
python3 compare_truth.py truth_4197761.json 4197761
```

實測結果：第 900、1357、1500、1682 章（39／41／42／195 段）全部逐段 100% 相符。

## 實測結果

《斗罗大陆IV终极斗罗》（`book/5626`）：

| 項目 | 結果 |
| --- | --- |
| 章節 | 1794 章（第 1–1793 章編號連續無缺，末尾〈後記〉） |
| 內文 | 4,312,144 字，平均 2,403 字/章（最短 1,906、最長 12,950） |
| 產出 | 7.12 MB、1802 個項目、EPUB 3 |
| 廣告殘留 | 0 段 |
| 抓取耗時 | 約 7 分鐘（6 執行緒、0.12s 間隔） |

另用 `book/1`《网游之近战法师》（章節網址規則不同）抽測 5 章，第 1 章 49/49 段相符，證明工具不只適用於單一本書。

## 注意

請自我克制：`--workers` 不要開太大，`--delay` 不要設 0。本工具預設值已經很保守，僅供個人離線閱讀與研究用途；請尊重著作權與網站服務條款。
