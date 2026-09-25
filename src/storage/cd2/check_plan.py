"""
计划「体检」（只读）—— 执行前扫一遍计划，揪出可能被误切进标题的技术残留。

为什么需要它：机械转换靠「技术令牌识别」切分，词表必然有覆盖不到的写法
（真实踩到过 `HD1080P` 这种变体，结果它被吞进了集标题）。
本脚本用「关键词 + 形状」双线索扫描每个新名字的「集标题」段：
  · 含已知技术关键词（WEB-DL / BluRay / HEVC / DDP / FLAC / 1080p / 10bit …）
  · 或是「纯 ASCII 且带数字/全大写」的可疑 token（中文标题里不该出现）
只要命中就打印出来交人工过目 —— 它是**提示**，不自动改任何东西。

用法：
    python check_plan.py plans/X.json [plans/Y.json ...]
    python check_plan.py plans/*.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

KEYWORDS = re.compile(
    r"(HD\d{3,4}[Pi]|FHD|UHD|\d{3,4}[pPiI]\b|WEB-?DL|WEBRip|Blu-?Ray|BDRip|BRRip|"
    r"REMUX|HDTV|HEVC|H\.?26[45]|AVC|[xX]26[45]|AV1|AAC|AC3|EAC3|DDP|DD\+|DTS|"
    r"TrueHD|Atmos|FLAC|MP3|PCM|Opus|\d{1,2}bit|DoVi|\bDV\b|HDR\d*\+?|HLG|SDR|"
    r"\d+Audio|VOSTFR|REPACK|PROPER|MULTi|Dual)", re.I)
CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def suspicious_tokens(title: str) -> list[str]:
    out = []
    for t in re.split(r"[.\s_]+", title):
        if not t or CJK.search(t):
            continue
        # ★ 纯数字 token 一律放过：技术字段必带单位（1080p / 10bit / 5.1 / x264 / H.265），
        #   光一个数字不可能是技术信息。真实反例：某剧 S02E01「737」、S05E04「51」、
        #   某剧 S06E05「恶魔 79」—— 都是货真价实的数字型集标题，曾被误报。
        if re.fullmatch(r"\d+", t):
            continue
        if KEYWORDS.search(t):
            out.append(t)
            continue
        # 纯 ASCII 且（带数字 或 全大写且长度>=2）→ 中文标题里不该有，提示人工看
        if re.fullmatch(r"[A-Za-z0-9+\-.]+", t) and len(t) >= 2:
            if any(c.isdigit() for c in t) or (t.isupper() and t not in ("SP", "OP", "ED", "PV")):
                out.append(t)
    return out


def main():
    ap = argparse.ArgumentParser(description="重命名计划体检（只读）")
    ap.add_argument("plans", nargs="+", help="一个或多个计划 JSON")
    args = ap.parse_args()

    paths: list[str] = []
    for pat in args.plans:
        paths += sorted(glob.glob(pat)) or [pat]

    bad_total = 0
    for path in paths:
        if not os.path.isfile(path):
            print(f"跳过（不存在）：{path}")
            continue
        plan = json.load(open(path, encoding="utf-8"))
        hits = []
        for op in plan.get("operations", []):
            m = re.search(r"「(.+)」", op.get("new_name", ""))
            if not m:
                continue
            bad = suspicious_tokens(m.group(1))
            if bad:
                hits.append((op["path"].rsplit("/", 1)[-1], op["new_name"], bad))
        bad_total += len(hits)
        mark = "✔" if not hits else "⚠"
        print(f"{mark} {os.path.basename(path)}：{len(plan.get('operations', []))} 条，可疑 {len(hits)} 条")
        for old, new, bad in hits[:10]:
            print(f"    可疑 {bad}")
            print(f"      {old}")
            print(f"      → {new}")
        if len(hits) > 10:
            print(f"    …其余 {len(hits) - 10} 条")
    print(f"\n合计可疑：{bad_total} 条" + ("（建议先修词表再执行）" if bad_total else "（可以放心执行）"))
    sys.exit(1 if bad_total else 0)


if __name__ == "__main__":
    main()
