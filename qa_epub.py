#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_epub.py — 對 hetushu2epub 產出的 EPUB 做完整質檢。

檢查項目：
  1. EPUB 容器結構（mimetype 位置/壓縮方式、container.xml、每個 XML 是否合法）
  2. manifest / spine 是否涵蓋所有章節檔，nav 連結是否都能對到實體檔
  3. 每章是否有標題、字數是否合理（找出異常短的章節）
  4. 正文是否殘留「和圖書」廣告字樣（含全形變體）
  5. 段落是否出現異常重複（段落順序還原失敗的典型症狀）

用法：python3 qa_epub.py <book.epub>
"""
from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

NS = {"opf": "http://www.idpf.org/2007/opf",
      "x": "http://www.w3.org/1999/xhtml",
      "ncx": "http://www.daisy.org/z3986/2005/ncx/"}

AD_PAT = re.compile(
    r"hetushu|hetubook|和[_\-\s]?图[_\-\s]?书|和[_\-\s]?圖[_\-\s]?書"
    r"|ｈ[ｔt]{2}ｐ|www\.|\.com|•", re.I)


def main(path: str) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

        # ---------- 1. 容器結構 ----------
        if names[0] != "mimetype":
            problems.append(f"mimetype 不是第一個項目（實際：{names[0]}）")
        if zf.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype 被壓縮（必須是 STORED）")
        if zf.read("mimetype") != b"application/epub+zip":
            problems.append("mimetype 內容不對")

        bad_xml = []
        for n in names:
            if n.endswith((".xml", ".opf", ".xhtml", ".ncx", ".html")):
                try:
                    ET.fromstring(zf.read(n))
                except Exception as exc:  # noqa: BLE001
                    bad_xml.append(f"{n}: {exc}")
        problems.extend(f"XML 不合法 → {b}" for b in bad_xml)

        # ---------- 2. manifest / spine / nav ----------
        opf = ET.fromstring(zf.read("OEBPS/content.opf"))
        manifest = {it.get("id"): it.get("href")
                    for it in opf.find("opf:manifest", NS)}
        spine = [ref.get("idref") for ref in opf.find("opf:spine", NS)]

        missing = [manifest[i] for i in spine
                   if "OEBPS/" + manifest.get(i, "") not in names]
        if missing:
            problems.append(f"spine 指向不存在的檔案：{missing[:5]}")

        chapter_files = sorted(n for n in names if n.startswith("OEBPS/text/"))
        in_spine = [i for i in spine if str(i).startswith("ch")]
        if len(chapter_files) != len(in_spine):
            problems.append(f"章節檔 {len(chapter_files)} 個，但 spine 只有 {len(in_spine)} 個")

        # nav 連結（只看 toc，landmarks 是另一份短清單）
        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
        toc_block = re.search(r'<nav epub:type="toc".*?</nav>', nav, re.S)
        nav_links = re.findall(r'href="(text/ch\d+\.xhtml)"',
                               toc_block.group(0) if toc_block else nav)
        if len(nav_links) != len(chapter_files):
            problems.append(f"nav 目錄 {len(nav_links)} 筆，章節檔 {len(chapter_files)} 個")
        dangling = [l for l in nav_links if "OEBPS/" + l not in names]
        if dangling:
            problems.append(f"nav 有斷鏈：{dangling[:5]}")

        ncx = zf.read("OEBPS/toc.ncx").decode("utf-8")
        ncx_links = re.findall(r'<content src="([^"]+)"/>', ncx)
        if len(ncx_links) - 1 != len(chapter_files):
            warnings.append(f"ncx 有 {len(ncx_links)} 筆（含書名頁），章節 {len(chapter_files)}")

        # ---------- 3~5. 逐章內容 ----------
        lengths: list[int] = []
        ad_hits: list[tuple[str, str]] = []
        dup_hits: list[tuple[str, str]] = []
        no_title = []
        titles: list[str] = []

        for n in chapter_files:
            doc = zf.read(n).decode("utf-8")
            m = re.search(r'<h1 class="chapter-title">(.*?)</h1>', doc, re.S)
            if not m or not m.group(1).strip():
                no_title.append(n)
            else:
                titles.append(m.group(1).strip())

            paras = re.findall(r"<p>(.*?)</p>", doc, re.S)
            lengths.append(sum(len(p) for p in paras))

            for p in paras:
                if AD_PAT.search(p):
                    ad_hits.append((n, p[:60]))

            # 相鄰段落完全重複 → 順序表解讀錯了
            for a, b in zip(paras, paras[1:]):
                if a and a == b and len(a) > 15:
                    dup_hits.append((n, a[:50]))
                    break

        if no_title:
            problems.append(f"{len(no_title)} 章沒有標題，例如 {no_title[:3]}")
        if ad_hits:
            problems.append(f"{len(ad_hits)} 段殘留廣告字樣，例如 {ad_hits[:3]}")
        if dup_hits:
            problems.append(f"{len(dup_hits)} 章出現相鄰重複段落，例如 {dup_hits[:3]}")

        # 重複章節標題（同一標題出現多次 → 抓錯/重抓）
        dup_titles = [t for t, c in Counter(titles).items() if c > 1]
        if dup_titles:
            warnings.append(f"有 {len(dup_titles)} 個重複標題，例如 {dup_titles[:3]}")

        tiny = [(n, l) for n, l in zip(chapter_files, lengths) if l < 200]
        if tiny:
            warnings.append(f"{len(tiny)} 章字數 <200，例如 {tiny[:3]}")

    # ---------- 報告 ----------
    import os
    total = sum(lengths)
    print(f"檔案      : {path}  ({os.path.getsize(path)/1048576:.2f} MB)")
    print(f"章節      : {len(chapter_files)}")
    print(f"內文字數  : {total:,}（平均 {total//max(1,len(lengths)):,} 字/章）")
    print(f"最短/最長 : {min(lengths):,} / {max(lengths):,} 字")
    print(f"廣告殘留  : {len(ad_hits)} 段")

    if warnings:
        print("\n警告：")
        for w in warnings:
            print("  ⚠", w)
    if problems:
        print("\n問題：")
        for p in problems:
            print("  ✗", p)
        return 1
    print("\n✓ 全部檢查通過")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
