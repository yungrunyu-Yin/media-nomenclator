"""
只读审计：逐集探测视频的**内封字幕**情况，并判断哪些集「真正缺字幕」。

判定逻辑（三者结合，缺一不可）：
    · 容器内字幕轨  → 有则算「有字幕」（含语言，重点看有没有简体 chi）
    · 同目录外挂字幕 → 有则算「有字幕」
    · 两者都没有    → 标 `缺字幕`，这才是真的需要补

用 `probe_tracks.py` 的 EBML 解析，**只读文件头，不下载视频正文**。

用法：
    python audit_subs.py --root /media/tv/shows
    python audit_subs.py --root /media/anime --sample 1
"""

from __future__ import annotations

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client  # noqa: E402
from probe_tracks import probe  # noqa: E402
from audit_v2 import skip_dir  # noqa: E402

# 挂载盘盘符（只读探测内封轨道时走本地文件；云端路径会映射到该盘符下）
MOUNT = os.environ.get("CD2_MOUNT", "F:")
VIDEO_EXT = ("mkv", "mp4", "avi", "mov", "ts")
SUB_EXT = ("ass", "srt", "ssa", "sub", "vtt", "smi")

# 分类目录下若还有一层分类（如 剧集/国产剧/美剧），在此指定
TV_ROOT = os.environ.get("MEDIA_TV_ROOT", "/media/tv")

SIMP_MARK = ("simplified", "chs", "简", "sc")
TRAD_MARK = ("traditional", "cht", "繁", "tc")


def sub_kinds(tracks: list[dict]) -> tuple[bool, bool, int]:
    """返回 (有简体, 有繁体, 字幕轨总数)"""
    subs = [t for t in tracks if t["type"] in (17, 18)]
    simp = trad = False
    for t in subs:
        tag = f"{t['lang']} {t['name']}".lower()
        if any(m in tag for m in SIMP_MARK):
            simp = True
        if any(m in tag for m in TRAD_MARK):
            trad = True
    return simp, trad, len(subs)


def main():
    ap = argparse.ArgumentParser(description="内封字幕只读审计")
    ap.add_argument("--root", required=True, help="要审计的分类目录，如 /media/tv/shows")
    ap.add_argument("--sample", type=int, default=0,
                    help="每季只探测前 N 个文件（0 = 全部）")
    args = ap.parse_args()

    c = CD2Client()
    # 目录结构：<root>/作品/第X季 或 <root>/分类/作品/第X季（剧集类多一层分类）
    works = [w for w in c.list_dir(args.root, force_refresh=False) if w["isDir"]]
    if args.root == TV_ROOT:
        works = [w for w in works]  # 分类层
    stats: dict[str, list] = {}
    for w in works:
        seasons = [s for s in c.list_dir(w["path"], force_refresh=False) if s["isDir"]]
        if not seasons:
            seasons = [w]
        for s in seasons:
            files = [x for x in c.list_dir(s["path"], force_refresh=False)
                     if not x["isDir"]]
            vids = [x for x in files if x["name"].rsplit(".", 1)[-1].lower() in VIDEO_EXT]
            subs = [x for x in files if x["name"].rsplit(".", 1)[-1].lower() in SUB_EXT]
            if args.sample:
                vids = vids[:args.sample]
            if not vids:
                continue
            key = f"{w['name']}/{s['name']}" if s is not w else w["name"]
            rec = {"vids": 0, "simp": 0, "tradonly": 0, "none": 0,
                   "ext_sub": len(subs), "no_sub_at_all": []}
            for v in vids:
                local = v["path"].replace("/", "\\").replace("\\", os.sep)
                local = os.path.join(MOUNT + os.sep,
                                     v["path"].lstrip("/").replace("/", os.sep))
                r = probe(local)
                rec["vids"] += 1
                if not r["ok"] and not r["tracks"]:
                    rec["none"] += 1
                    if not subs:
                        rec["no_sub_at_all"].append(v["name"])
                    continue
                simp, trad, n = sub_kinds(r["tracks"])
                if simp:
                    rec["simp"] += 1
                elif n:
                    rec["tradonly"] += 1
                else:
                    rec["none"] += 1
                    if not subs:
                        rec["no_sub_at_all"].append(v["name"])
            stats[key] = rec
            print(f"  {key}: 探测 {rec['vids']} 集 ｜ 内封含简体 {rec['simp']}，"
                  f"仅非简体 {rec['tradonly']}，无内封字幕 {rec['none']} ｜ 外挂字幕 {rec['ext_sub']} 个"
                  + (f"  ⚠ 真正缺字幕 {len(rec['no_sub_at_all'])} 集" if rec["no_sub_at_all"] else ""))

    print("\n===== 汇总 =====")
    miss = sum(len(v["no_sub_at_all"]) for v in stats.values())
    print(f"共 {len(stats)} 个季；内封含简体的集数 "
          f"{sum(v['simp'] for v in stats.values())}；"
          f"**既无内封也无外挂（真正缺字幕）** {miss} 集")
    for k, v in stats.items():
        for n in v["no_sub_at_all"]:
            print(f"   缺字幕： {k} / {n}")
    c.close()


if __name__ == "__main__":
    main()
