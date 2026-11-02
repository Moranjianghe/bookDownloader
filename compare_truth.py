#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare_truth.py — 用瀏覽器渲染結果驗證工具的輸出。

標準答案取得方式（在瀏覽器 console 或 Playwright 執行）：

    [...document.querySelector('#content').children]
      .filter(el => el.tagName === 'DIV' && getComputedStyle(el).display !== 'none')
      .map(el => el.innerText.trim())

把它存成 JSON 陣列，再執行：

    python3 compare_truth.py truth.json 4197761
"""
from __future__ import annotations

import json
import re
import sys

import hetushu2epub as H


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    truth_path, cid = sys.argv[1], sys.argv[2]

    truth = json.load(open(truth_path, encoding="utf-8"))
    fetcher = H.Fetcher(delay=0.2)
    book = H.fetch_book(fetcher, "5626")
    chapter = next((c for c in book.chapters if c.cid == cid), None)
    if chapter is None:
        print(f"找不到章節 {cid}")
        return 2
    H.fetch_chapter(fetcher, book, chapter, None)

    mine = chapter.paragraphs
    print(f"章節   : {chapter.title}")
    print(f"瀏覽器 : {len(truth)} 段")
    print(f"工具   : {len(mine)} 段")

    same = sum(1 for a, b in zip(truth, mine) if norm(a) == norm(b))
    print(f"逐段相符: {same}/{len(truth)}")

    diffs = [(i, a, b) for i, (a, b) in enumerate(zip(truth, mine)) if norm(a) != norm(b)]
    if diffs or len(truth) != len(mine):
        print("\n差異：")
        for i, a, b in diffs[:5]:
            print(f"  [{i}] 瀏覽器: {a[:80]}")
            print(f"      工具  : {b[:80]}")
        return 1
    print("\n✓ 與瀏覽器渲染結果完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
