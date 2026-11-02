#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hetushu2epub.py — 將「和圖書」(hetushu.com) 的小說抓取並打包成 EPUB 3 電子書。

只用 Python 標準函式庫，無需安裝任何依賴。

用法:
    python3 hetushu2epub.py https://www.hetushu.com/book/5626/
    python3 hetushu2epub.py 5626 -o 斗羅大陸IV終極斗羅.epub
    python3 hetushu2epub.py 5626 --list          # 只列出目錄
    python3 hetushu2epub.py 5626 --start 1 --end 20   # 只抓前 20 章

網站結構重點（本工具已處理）:
  * 書目頁  /book/<bid>/             → 書名、作者、簡介、封面、章節清單
  * 章節頁  /book/<bid>/<cid>.html   → 章節標題 + 被打亂順序的段落
  * 校正端點 /book/<bid>/r<cid>.json → 回 204，並在 `token` 標頭給出「段落順序表」
       token = base64( 例如 "34E%24E%5Z..." )，以 /[A-Z]+%/ 切開即得每個段落的正確序號；
       序號 < 5 的段落佔用該絕對位置，其餘依序遞補（與網頁 section.js 的行為一致）。
  * 段落中夾帶的 <bdo>/<tt>/<s>/... 標籤是「和圖書」的網址廣告，需連同內容一併移除。
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 常數
# --------------------------------------------------------------------------

BASE = "https://www.hetushu.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 站方自己的 CSS（/command/section.css）是用這條規則決定「讀者看不到什麼」：
#
#   #content acronym,#content bdo,#content big,#content cite,#content code,
#   #content dfn,#content kbd,#content q,#content s,#content samp,
#   #content strike,#content tt,#content u,#content var{display:none;}
#
# 換句話說，這 14 個標籤連同內容一律不顯示，就是本站的廣告位。
# 以它為唯一依據，比用關鍵字猜「這段字像不像廣告」可靠得多。
HIDDEN_TAGS = (
    "acronym", "bdo", "big", "cite", "code", "dfn", "kbd", "q",
    "s", "samp", "strike", "tt", "u", "var",
)

# 快取格式版本；演算法有變就 +1，舊快取會被自動重抓
CACHE_VERSION = 3

# 清理後的殘留檢查用（正常應為 0）
_AD_ASCII = re.compile(r"hetushu|hetubook")
_AD_CJK = re.compile(r"和[图圖][书書]")
_AD_CJK_FALSE = re.compile(r"[图圖][书書][馆館]")   # 「和图书馆」是正常詞
RESIDUE = {"count": 0, "examples": []}
_RESIDUE_LOCK = threading.Lock()


def log(msg: str) -> None:
    """輸出到 stderr，避免污染管線輸出。"""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Fetcher:
    """帶重試、限速與自訂標頭的 HTTP 抓取器。"""

    def __init__(self, ua: str = DEFAULT_UA, retries: int = 4, timeout: int = 30,
                 delay: float = 0.15):
        self.ua = ua
        self.retries = retries
        self.timeout = timeout
        self.delay = delay
        self._lock = threading.Lock()
        self._last = 0.0
        self.stats = {"requests": 0, "retries": 0, "failures": 0}

    def _throttle(self) -> None:
        """所有執行緒共用的最小間隔，避免對站方造成壓力。"""
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last + self.delay - now
            if wait > 0:
                time.sleep(wait)
                now += wait
            self._last = now

    def get(self, url: str, referer: Optional[str] = None,
            xhr: bool = False) -> Tuple[int, Dict[str, str], bytes]:
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            self._throttle()
            req = urllib.request.Request(url, headers={
                "User-Agent": self.ua,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8",
                "Connection": "close",
            })
            if referer:
                req.add_header("Referer", referer)
            if xhr:
                req.add_header("X-Requested-With", "XMLHttpRequest")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    with self._lock:
                        self.stats["requests"] += 1
                    return resp.status, headers, body
            except urllib.error.HTTPError as exc:
                # 403/429/5xx 視為暫時性錯誤
                if exc.code in (403, 429, 500, 502, 503, 504, 522, 524):
                    last_err = exc
                    with self._lock:
                        self.stats["retries"] += 1
                    time.sleep(min(2 ** attempt + random.uniform(0, 0.6), 20))
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 - 網路錯誤一律重試
                last_err = exc
                with self._lock:
                    self.stats["retries"] += 1
                time.sleep(min(2 ** attempt + random.uniform(0, 0.6), 20))
        with self._lock:
            self.stats["failures"] += 1
        raise RuntimeError(f"取得失敗: {url} ({last_err})")


# --------------------------------------------------------------------------
# 資料模型
# --------------------------------------------------------------------------


@dataclass
class Chapter:
    index: int          # 1-based 章號
    cid: str            # 章節 id
    title: str
    url: str
    paragraphs: List[str] = field(default_factory=list)

    @property
    def text_length(self) -> int:
        return sum(len(p) for p in self.paragraphs)


@dataclass
class Book:
    bid: str
    title: str
    author: str
    intro: str
    cover_url: Optional[str]
    chapters: List[Chapter]

    @property
    def char_count(self) -> int:
        return sum(c.text_length for c in self.chapters)


# --------------------------------------------------------------------------
# 文字清理
# --------------------------------------------------------------------------


def _normalize_for_ad_check(text: str) -> str:
    """把文字壓成「只留英數與漢字」，用來回頭檢查有沒有漏網的廣告。

    站方的廣告會用全形字、大小寫、`_ - * • . 空格` 等任意分隔符穿插，
    例如 `和*图*书`、`hｔtps://ｍ•hｅｔusｈu•ｃoｍ`。
    NFKC 把全形轉半形後，再剔除所有非英數／非漢字字元，就能一次收斂。
    """
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", t)


def looks_like_ad(text: str) -> bool:
    """文字裡是否還帶著站名／網址（只用於清理後的自我檢查）。"""
    norm = _normalize_for_ad_check(text)
    if not norm:
        return False
    if _AD_ASCII.search(norm):
        return True
    return bool(_AD_CJK.search(norm)) and not _AD_CJK_FALSE.search(norm)


def _note_residue(text: str) -> None:
    """清理後仍含廣告字樣時記錄下來（正常情況下應該是 0）。"""
    if looks_like_ad(text):
        with _RESIDUE_LOCK:
            RESIDUE["count"] += 1
            if len(RESIDUE["examples"]) < 5:
                RESIDUE["examples"].append(text[:60])


# 站方隱藏的元素（含內容）——依 CSS 規則，出現即代表「讀者看不到」
_HIDDEN_ELEMENT_RE = re.compile(
    r"<(%s)\b[^>]*>.*?</\1\s*>" % "|".join(HIDDEN_TAGS), re.S | re.I
)
_TAG_RE = re.compile(r"<[^<>]+>", re.S)


def clean_paragraphs(raw_paras: Sequence[str]) -> List[str]:
    """移除站方設定為隱藏的廣告元素，並把其餘行內標籤轉為純文字。"""
    result: List[str] = []
    for raw in raw_paras:
        text = raw

        # 1) 刪掉站方 CSS 隱藏的標籤（本站的廣告位）——連內容一起丟棄。
        #    原本用「內容像不像廣告」來判斷，但站方會把站名拆成
        #    「和」「*图*」「书」等片段干擾比對，改依 CSS 就沒有這個問題。
        text = _HIDDEN_ELEMENT_RE.sub("", text)

        # 2) 其餘標籤（多為無害的行內標記）一律拆掉，只留文字
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = _TAG_RE.sub("", text)
        text = html.unescape(text)

        # 3) 收尾：去零寬字元、壓縮空白
        text = text.replace("\u200b", "").replace("\ufeff", "")
        text = re.sub(r"[ \t\u00a0]+", " ", text).strip()
        if text:
            _note_residue(text)
            result.append(text)
    return result


# --------------------------------------------------------------------------
# 解析：書目頁 / 章節頁 / token
# --------------------------------------------------------------------------

RE_CHAPTER_LINK = re.compile(
    r'<dd><a\s+href="(/book/(\d+)/(\d+)\.html)"\s+title="([^"]*)"', re.I
)
RE_TITLE_TAG = re.compile(r"<title>(.*?)</title>", re.S | re.I)
RE_BOOK_TITLE = re.compile(r'<div class="book_info[^"]*">.*?<h2>(.*?)</h2>', re.S)
RE_AUTHOR = re.compile(r'作者：<a[^>]*>(.*?)</a>', re.S)
RE_INTRO = re.compile(r'<div class="intro">(.*?)</div>', re.S)
RE_COVER = re.compile(r'<div class="book_info[^"]*">\s*<img[^>]+src="([^"]+)"', re.S)
RE_CTITLE = re.compile(r'<div id="ctitle">.*?<div class="title">(.*?)</div>', re.S)
RE_H2 = re.compile(r'<h2 class="h2">(.*?)</h2>', re.S)


def fetch_book(fetcher: Fetcher, book_ref: str) -> Book:
    """讀取書目頁，取得metadata與章節清單。book_ref 可為數字 id 或完整網址。"""
    if book_ref.isdigit():
        url = f"{BASE}/book/{book_ref}/"
    else:
        url = book_ref
        if not url.endswith("/"):
            url += "/"
    bid = re.search(r"/book/(\d+)", url)
    if not bid:
        raise SystemExit(f"無法從網址解析書籍 id: {url}")
    bid = bid.group(1)

    _status, _headers, body = fetcher.get(url)
    page = body.decode("utf-8", "replace")

    title = ""
    m = RE_BOOK_TITLE.search(page)
    if m:
        title = _strip_html(m.group(1))
    if not title:
        m = RE_TITLE_TAG.search(page)
        if m:
            title = _strip_html(m.group(1)).split("_")[0]
    if not title:
        raise SystemExit("找不到書名，網址可能不是書籍頁。")

    m = RE_AUTHOR.search(page)
    author = _strip_html(m.group(1)) if m else ""

    intro = ""
    m = RE_INTRO.search(page)
    if m:
        intro = _strip_html(m.group(1), keep_breaks=True)

    cover_url = None
    m = RE_COVER.search(page)
    if m:
        cover_url = urllib.parse.urljoin(BASE, m.group(1))

    chapters: List[Chapter] = []
    seen = set()
    for path, cbid, cid, ctitle in RE_CHAPTER_LINK.findall(page):
        if cbid != bid or cid in seen:
            continue
        seen.add(cid)
        chapters.append(Chapter(
            index=len(chapters) + 1,
            cid=cid,
            title=_strip_html(ctitle) or f"第 {len(chapters) + 1} 章",
            url=urllib.parse.urljoin(BASE, path),
        ))

    if not chapters:
        raise SystemExit("目錄為空，無法繼續。")

    return Book(bid=bid, title=title, author=author, intro=intro,
                cover_url=cover_url, chapters=chapters)


def _strip_html(fragment: str, keep_breaks: bool = False) -> str:
    """把一小段 HTML 轉純文字。"""
    text = fragment
    if keep_breaks:
        text = re.sub(r"</p\s*>|<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


RE_CONTENT_BLOCK = re.compile(
    r'<div id="content"[^>]*>(.*?)<div id="console"', re.S | re.I
)


class _ParagraphExtractor(HTMLParser):
    """取出 <div id="content"> 的「直接子層 div」清單（保留其內部原始 HTML）。

    正規表达式在段落內含巢狀 div 時會出錯，這裡用事件式解析，並跳過
    `<div class="mask">`（站方的防拷貝遮罩，不是正文）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.depth = 0            # 目前開啟中的 div 層數
        self.capture: Optional[List[str]] = None
        self.paras: List[str] = []
        self.skipping = 0         # >0 表示正在跳過 mask 之類的非正文容器
        self.images = 0

    def _emit(self, chunk: str) -> None:
        if self.capture is not None and not self.skipping:
            self.capture.append(chunk)

    def handle_starttag(self, tag: str, attrs) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        if tag == "div":
            if self.depth == 0 and self.capture is None and not self.skipping:
                classes = (dict(attrs).get("class") or "").split()
                if "mask" in classes:
                    self.skipping = 1
                else:
                    self.capture = []
            elif self.skipping:
                self.skipping += 1
            self.depth += 1
            self._emit(raw)
            return
        if tag == "img":
            self.images += 1
        self._emit(raw)

    def handle_startendtag(self, tag: str, attrs) -> None:
        raw = self.get_starttag_text() or f"<{tag}/>"
        if tag == "img":
            self.images += 1
        self._emit(raw)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            self.depth -= 1
            if self.skipping:
                self.skipping -= 1
                if self.skipping == 0:
                    self.depth = 0
                return
            if self.depth == 0 and self.capture is not None:
                self.paras.append("".join(self.capture))
                self.capture = None
                return
        self._emit(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def handle_entityref(self, name: str) -> None:
        self._emit(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._emit(f"&#{name};")


def parse_chapter_html(page: str) -> Tuple[str, List[str], int]:
    """從章節頁取出標題與（仍為亂序的）原始段落 HTML。"""
    title = ""
    m = RE_CTITLE.search(page)
    if m:
        title = _strip_html(m.group(1))
    if not title:
        m = RE_H2.search(page)
        if m:
            title = _strip_html(m.group(1))

    m = RE_CONTENT_BLOCK.search(page)
    if not m:
        return title, [], 0

    parser = _ParagraphExtractor()
    parser.feed(m.group(1))
    parser.close()
    return title, parser.paras, parser.images


def decode_order(token: str) -> List[int]:
    """把 token 解碼成「段落位置表」。

    token = base64("34E%24E%5Z%...")；以 /[A-Z]+%/ 切開後，每個數字代表
    該段落應去的絕對位置。< 5 的位置優先佔用，其餘依序塞入剩下的空位。
    """
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(padded, validate=False).decode("latin-1")
    except Exception:  # noqa: BLE001
        return []
    parts = [p for p in re.split(r"[A-Z]+%", raw) if p.strip()]
    order: List[int] = []
    for p in parts:
        try:
            order.append(int(p))
        except ValueError:
            return []
    return order


def reorder(paras: Sequence[str], order: Sequence[int]) -> List[str]:
    """依 token 的順序表重排段落（對應 section.js 的還原邏輯）。"""
    if not order or len(order) != len(paras):
        return list(paras)          # 對不上就維持原樣，至少不丟內容
    slots: Dict[int, str] = {}
    filled = 0
    for i, pos in enumerate(order):
        if pos < 5:
            slots[pos] = paras[i]
            filled += 1
        else:
            slots[pos - filled] = paras[i]
    out = [slots[i] for i in range(len(paras)) if i in slots]
    return out if len(out) == len(paras) else list(paras)


# --------------------------------------------------------------------------
# 抓取章節
# --------------------------------------------------------------------------


def fetch_chapter(fetcher: Fetcher, book: Book, ch: Chapter,
                  cache_dir: Optional[str]) -> Chapter:
    """抓一章：先看快取，否則取 HTML + token、還原順序、清理文字。"""
    cache_path = os.path.join(cache_dir, f"{ch.index:05d}_{ch.cid}.json") if cache_dir else None
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("v") == CACHE_VERSION:
                ch.title = data.get("title") or ch.title
                ch.paragraphs = data.get("paragraphs") or []
                if ch.paragraphs:
                    return ch
        except Exception:  # noqa: BLE001 - 快取壞掉就重抓
            pass

    _st, _hd, body = fetcher.get(ch.url)
    page = body.decode("utf-8", "replace")
    title, raw_paras, n_images = parse_chapter_html(page)
    if n_images:
        log(f"  i 第 {ch.index} 章含 {n_images} 張圖片（目前只保留文字）")
    if title:
        ch.title = title

    order: List[int] = []
    try:
        _s2, headers, jbody = fetcher.get(
            f"{BASE}/book/{book.bid}/r{ch.cid}.json", referer=ch.url, xhr=True)
        token = headers.get("token")
        if token:
            order = decode_order(token)
        elif jbody:
            # 少數情況會直接回 200 + base64 內容（loadType=normal）
            order = decode_order(jbody.decode("latin-1").strip())
    except Exception as exc:  # noqa: BLE001
        log(f"  ! 第 {ch.index} 章取順序表失敗，改用原始順序：{exc}")

    ch.paragraphs = clean_paragraphs(reorder(raw_paras, order))

    if cache_path:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": CACHE_VERSION, "cid": ch.cid, "title": ch.title,
                       "paragraphs": ch.paragraphs}, fh, ensure_ascii=False)
        os.replace(tmp, cache_path)
    return ch


def fetch_all(fetcher: Fetcher, book: Book, cache_dir: Optional[str],
              workers: int) -> Book:
    """併發抓取所有章節，並顯示進度。"""
    total = len(book.chapters)
    done = 0
    failed: List[Chapter] = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_chapter, fetcher, book, ch, cache_dir): ch
                   for ch in book.chapters}
        for fut in as_completed(futures):
            ch = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                failed.append(ch)
                log(f"  ! 第 {ch.index} 章失敗：{exc}")
            done += 1
            if done % 10 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                log(f"  進度 {done}/{total}  ({rate:5.1f} 章/秒, 剩餘 ~{eta:4.0f}s)")

    # 失敗的章節至少留個佔位，避免整本書缺章而無人察覺
    for ch in failed:
        if not ch.paragraphs:
            ch.paragraphs = [f"（本章抓取失敗，請稍後重新執行以補齊：{ch.url}）"]

    log(f"  完成 {total - len(failed)}/{total} 章，耗時 {time.time() - t0:.0f}s")
    return book


# --------------------------------------------------------------------------
# EPUB 產生
# --------------------------------------------------------------------------

CSS = """@charset "utf-8";
html { font-size: 100%; }
body {
  margin: 0 5%;
  line-height: 1.75;
  text-align: justify;
  font-family: "Noto Serif CJK TC", "Source Han Serif TC", "Songti SC", serif;
}
h1.chapter-title {
  font-size: 1.25em;
  font-weight: bold;
  text-align: center;
  margin: 2.5em 0 1.5em;
  padding-bottom: 0.6em;
  border-bottom: 1px solid #ccc;
  page-break-before: always;
  break-before: page;
}
p { text-indent: 2em; margin: 0.55em 0; }
div.titlepage { text-align: center; margin-top: 15%; }
div.titlepage h1 { font-size: 2em; margin: 0.4em 0; }
div.titlepage p.author { text-indent: 0; font-size: 1.1em; color: #444; }
div.titlepage img.cover { max-width: 100%; max-height: 70vh; margin: 1.2em auto; display: block; }
div.intro { margin: 2em 0; padding: 1em 1.2em; border-left: 3px solid #bbb; background: #fafafa; }
div.intro p { text-indent: 0; font-size: 0.95em; color: #333; }
div.intro h2 { font-size: 1em; margin: 0 0 0.6em; color: #666; }
p.meta { text-indent: 0; text-align: center; color: #666; font-size: 0.9em; }
"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def xml_escape(text: str) -> str:
    return html.escape(text, quote=False)


def attr_escape(text: str) -> str:
    return html.escape(text, quote=True)


def chapter_xhtml(ch: Chapter) -> str:
    paras = "\n".join(f"    <p>{xml_escape(p)}</p>" for p in ch.paragraphs)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh" lang="zh">
<head>
  <meta charset="utf-8"/>
  <title>{xml_escape(ch.title)}</title>
  <link rel="stylesheet" type="text/css" href="../style.css"/>
</head>
<body>
  <section epub:type="chapter">
    <h1 class="chapter-title">{xml_escape(ch.title)}</h1>
{paras}
  </section>
</body>
</html>
"""


def titlepage_xhtml(book: Book, has_cover: bool) -> str:
    cover_img = ('\n    <img class="cover" src="images/cover.jpg" alt="封面"/>'
                 if has_cover else "")
    intro = ""
    if book.intro:
        body = "".join(f"<p>{xml_escape(line)}</p>" for line in book.intro.splitlines() if line.strip())
        intro = f'\n    <div class="intro"><h2>簡介</h2>{body}</div>'
    meta = "／".join(x for x in (book.author, f"共 {len(book.chapters)} 章") if x)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh" lang="zh">
<head>
  <meta charset="utf-8"/>
  <title>{xml_escape(book.title)}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <div class="titlepage">
    <h1>{xml_escape(book.title)}</h1>
    <p class="author">{xml_escape(book.author)}</p>{cover_img}
  </div>{intro}
  <p class="meta">{xml_escape(meta)}</p>
  <p class="meta">來源：hetushu.com</p>
</body>
</html>
"""


def nav_xhtml(book: Book, group_size: int = 100) -> str:
    """建立 EPUB3 導覽文件；章節多時以 100 章為一組做兩層目錄。"""
    def li(ch: Chapter) -> str:
        return (f'        <li><a href="text/ch{ch.index:05d}.xhtml">'
                f"{xml_escape(ch.title)}</a></li>")

    if group_size and len(book.chapters) > group_size:
        items = []
        for start in range(0, len(book.chapters), group_size):
            chunk = book.chapters[start:start + group_size]
            label = f"{chunk[0].title} ～ {chunk[-1].title}"
            sub = "\n".join(li(c) for c in chunk)
            items.append(f'      <li><span>{xml_escape(label)}</span>\n        <ol>\n{sub}\n        </ol>\n      </li>')
        toc = "\n".join(items)
    else:
        toc = "\n".join(li(c) for c in book.chapters)

    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh" lang="zh">
<head>
  <meta charset="utf-8"/>
  <title>目錄</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>目錄</h1>
    <ol>
      <li><a href="titlepage.xhtml">書名頁</a></li>
{toc}
    </ol>
  </nav>
  <nav epub:type="landmarks" id="landmarks" hidden="hidden">
    <h2>導覽</h2>
    <ol>
      <li><a epub:type="toc" href="nav.xhtml">目錄</a></li>
      <li><a epub:type="bodymatter" href="text/ch00001.xhtml">正文</a></li>
    </ol>
  </nav>
</body>
</html>
"""


def ncx_xml(book: Book, book_id: str, group_size: int = 100) -> str:
    """EPUB2 相容用的 NCX（扁平目錄）。"""
    points = []
    depth = 1

    def nav_point(ch: Chapter, idx: int, order_no: int) -> str:
        return (f'    <navPoint id="np{idx}" playOrder="{order_no}">\n'
                f"      <navLabel><text>{xml_escape(ch.title)}</text></navLabel>\n"
                f'      <content src="text/ch{ch.index:05d}.xhtml"/>\n'
                "    </navPoint>")

    order_no = 1
    points.append(f'    <navPoint id="np_title" playOrder="{order_no}">\n'
                  f"      <navLabel><text>書名頁</text></navLabel>\n"
                  '      <content src="titlepage.xhtml"/>\n'
                  "    </navPoint>")
    for ch in book.chapters:
        order_no += 1
        points.append(nav_point(ch, ch.index, order_no))
    _ = depth

    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh">
  <head>
    <meta name="dtb:uid" content="{attr_escape(book_id)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{xml_escape(book.title)}</text></docTitle>
  <docAuthor><text>{xml_escape(book.author)}</text></docAuthor>
  <navMap>
{chr(10).join(points)}
  </navMap>
</ncx>
"""


def opf_xml(book: Book, book_id: str, has_cover: bool,
            modified: str) -> str:
    items = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="css" href="style.css" media-type="text/css"/>',
        '    <item id="titlepage" href="titlepage.xhtml" media-type="application/xhtml+xml"/>',
    ]
    if has_cover:
        items.append('    <item id="cover-image" href="images/cover.jpg" '
                     'media-type="image/jpeg" properties="cover-image"/>')
    for ch in book.chapters:
        items.append(f'    <item id="ch{ch.index}" href="text/ch{ch.index:05d}.xhtml" '
                     'media-type="application/xhtml+xml"/>')

    spine = ['    <itemref idref="titlepage"/>', '    <itemref idref="nav" linear="yes"/>']
    spine += [f'    <itemref idref="ch{ch.index}"/>' for ch in book.chapters]

    desc = ""
    if book.intro:
        desc = f"    <dc:description>{xml_escape(book.intro)}</dc:description>\n"

    meta_cover = '    <meta name="cover" content="cover-image"/>\n' if has_cover else ""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{attr_escape(book_id)}</dc:identifier>
    <dc:title>{xml_escape(book.title)}</dc:title>
    <dc:creator>{xml_escape(book.author)}</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:source>{attr_escape(BASE + '/book/' + book.bid + '/')}</dc:source>
{desc}    <meta property="dcterms:modified">{modified}</meta>
{meta_cover}  </metadata>
  <manifest>
{chr(10).join(items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine)}
  </spine>
</package>
"""


def write_epub(book: Book, out_path: str, fetcher: Optional[Fetcher] = None,
               group_size: int = 100) -> str:
    """把 Book 寫成 EPUB 3 檔案。"""
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, BASE + '/book/' + book.bid)}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cover_bytes: Optional[bytes] = None
    if book.cover_url and fetcher is not None:
        try:
            _s, _h, cover_bytes = fetcher.get(book.cover_url, referer=f"{BASE}/book/{book.bid}/")
            if cover_bytes and cover_bytes[:2] != b"\xff\xd8":
                cover_bytes = None      # 不是 JPEG 就不放，避免格式不符
        except Exception as exc:  # noqa: BLE001
            log(f"  ! 封面下載失敗（略過）：{exc}")
            cover_bytes = None

    has_cover = cover_bytes is not None

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    tmp_path = out_path + ".part"
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype 必須是第一個且不壓縮
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/style.css", CSS)
        zf.writestr("OEBPS/titlepage.xhtml", titlepage_xhtml(book, has_cover))
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml(book, group_size))
        zf.writestr("OEBPS/toc.ncx", ncx_xml(book, book_id, group_size))
        zf.writestr("OEBPS/content.opf", opf_xml(book, book_id, has_cover, modified))
        if has_cover:
            zf.writestr("OEBPS/images/cover.jpg", cover_bytes)
        for ch in book.chapters:
            zf.writestr(f"OEBPS/text/ch{ch.index:05d}.xhtml", chapter_xhtml(ch))

    os.replace(tmp_path, out_path)
    return out_path


# --------------------------------------------------------------------------
# 驗證
# --------------------------------------------------------------------------


def verify_epub(path: str, expect_chapters: Optional[int] = None) -> Dict[str, object]:
    """做基本健檢：zip 結構、mimetype、XML 是否合法、章節數。"""
    import xml.etree.ElementTree as ET

    problems: List[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if names[0] != "mimetype":
            problems.append("mimetype 不是第一個檔案")
        info = zf.getinfo("mimetype")
        if info.compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype 被壓縮了")
        if zf.read("mimetype") != b"application/epub+zip":
            problems.append("mimetype 內容錯誤")
        if "META-INF/container.xml" not in names:
            problems.append("缺少 container.xml")

        xml_files = [n for n in names if n.endswith((".xml", ".opf", ".xhtml", ".ncx"))]
        for n in xml_files:
            try:
                ET.fromstring(zf.read(n))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{n} XML 不合法: {exc}")

        chapters = [n for n in names if n.startswith("OEBPS/text/")]
        if expect_chapters is not None and len(chapters) != expect_chapters:
            problems.append(f"章節檔數 {len(chapters)} != 預期 {expect_chapters}")

        total_text = 0
        for n in chapters:
            total_text += len(zf.read(n))
        size = os.path.getsize(path)

    return {
        "size_bytes": size,
        "chapters": len(chapters),
        "xml_files": len(xml_files),
        "markup_bytes": total_text,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hetushu2epub",
        description="把 hetushu.com 的小說抓下來做成 EPUB 3。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例:
  python3 hetushu2epub.py https://www.hetushu.com/book/5626/
  python3 hetushu2epub.py 5626 -o 終極斗羅.epub --workers 6
  python3 hetushu2epub.py 5626 --list
  python3 hetushu2epub.py 5626 --start 1 --end 50 -o 試讀.epub
""",
    )
    p.add_argument("book", help="書籍 id 或書目頁網址，例如 5626 或 https://www.hetushu.com/book/5626/")
    p.add_argument("-o", "--output", help="輸出 .epub 路徑（預設 <書名>.epub）")
    p.add_argument("--start", type=int, default=1, help="起始章號（1 起算，預設 1）")
    p.add_argument("--end", type=int, default=0, help="結束章號（含，預設全部）")
    p.add_argument("--list", action="store_true", help="只列出目錄，不抓內文")
    p.add_argument("--workers", type=int, default=5, help="併發數（預設 5，請勿過高）")
    p.add_argument("--delay", type=float, default=0.15, help="每次請求的最小間隔秒數（預設 0.15）")
    p.add_argument("--cache-dir", default=None, help="章節快取目錄（預設 ./cache/<bid>，可續傳）")
    p.add_argument("--no-cache", action="store_true", help="不快取，全部重抓")
    p.add_argument("--nav-group", type=int, default=100, help="目錄每幾章分一組（0 = 不分組）")
    p.add_argument("--no-verify", action="store_true", help="跳過產出後的健檢")
    p.add_argument("--user-agent", default=DEFAULT_UA, help="自訂 User-Agent")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    fetcher = Fetcher(ua=args.user_agent, delay=args.delay)

    log(f"→ 讀取書目：{args.book}")
    book = fetch_book(fetcher, args.book)
    log(f"  《{book.title}》／{book.author}／共 {len(book.chapters)} 章")

    if args.list:
        for ch in book.chapters:
            print(f"{ch.index:5d}  {ch.cid}  {ch.title}")
        return 0

    start = max(1, args.start)
    end = args.end if args.end > 0 else len(book.chapters)
    end = min(end, len(book.chapters))
    if start > end:
        log(f"章號範圍無效：{start}-{end}")
        return 2
    selected = book.chapters[start - 1:end]
    for i, ch in enumerate(selected, start=1):
        ch.index = i                       # 重新編號，讓輸出的 epub 自成一體
    book.chapters = selected

    out_path = args.output or f"{book.title}.epub"
    if not out_path.lower().endswith(".epub"):
        out_path += ".epub"

    cache_dir = None if args.no_cache else (
        args.cache_dir or os.path.join("cache", book.bid)
    )
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        log(f"  快取目錄：{cache_dir}")

    log(f"→ 開始抓取 {len(book.chapters)} 章（{args.workers} 執行緒）")
    fetch_all(fetcher, book, cache_dir, args.workers)

    log("→ 產生 EPUB…")
    write_epub(book, out_path, fetcher=fetcher, group_size=args.nav_group)

    if not args.no_verify:
        info = verify_epub(out_path, expect_chapters=len(book.chapters))
        log(f"  檔案：{out_path}  {info['size_bytes'] / 1048576:.2f} MB")
        log(f"  章節：{info['chapters']}  XML 檔：{info['xml_files']}")
        if info["problems"]:
            for prob in info["problems"]:
                log(f"  ✗ {prob}")
            return 1
        log("  ✓ 結構檢查通過")

    log(f"✓ 完成：{out_path}（{book.char_count:,} 字）")
    print(os.path.abspath(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
