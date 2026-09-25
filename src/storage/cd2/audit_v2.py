"""
v2 规则落地审计（只读）—— 统计哪些文件已是规则 v2 格式、哪些还是旧格式。

规则 v2（2026-09-24）：
  剧集/动漫  `剧名.年份.SxxExx.「集标题」.ext`   ← 集标题后无任何技术信息
  电影       `片名 (年份) 分辨率.ext`             ← 只留年份 + 分辨率

用法：
    python audit_v2.py                      # 审计所有已配置的分类目录
    python audit_v2.py --root /media/anime  # 只审计某类
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client  # noqa: E402

MEDIA_EXT = ("mkv", "mp4", "avi", "mov", "ts",
             "ass", "srt", "ssa", "sub", "vtt", "smi")
# 番外/特典目录：按 Skill 规则整体不动，不参与审计
SKIP_DIR_WORDS = ("番外", "特别篇", "特别集", "花絮", "幕后", "SP", "Special",
                  "Specials", "OVA", "OAD", "Extra", "Fonts", "Scans", "menu")

RE_EP_V2 = re.compile(
    r"\.S\d{2}E\d{2}(-E\d{2})?"
    r"(?:\.「.+」)?"          # 集标题段可有（带「」）可无（无可靠标题 → 整段省略）
    r"\.[A-Za-z0-9]+$")
# 旧格式 = SxxExx 之后除了「可选的一段「标题」」还有多余字段
RE_EP_LEGACY = re.compile(
    r"\.S\d{2}E\d{2}(-E\d{2})?\..+\.(?:\d{3,4}p|WEB-?DL|WEBRip|Blu-?Ray|BDRip|"
    r"REMUX|HDTV|HEVC|H\.?26[45]|[xX]26[45]|AAC|DDP|FLAC|DTS|TrueHD)\b", re.I)
RE_MOVIE_V2 = re.compile(r"^.+ \(\d{4}\) \d{3,4}p\.[A-Za-z0-9]+$")


def is_media(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in MEDIA_EXT


def skip_dir(name: str) -> bool:
    low = name.lower()
    return any(w.lower() in low for w in SKIP_DIR_WORDS)


def walk_dirs(c: CD2Client, root: str, max_depth: int = 3) -> list[str]:
    """递归收集作品子目录（跳过番外/特典目录）。root 本身的第一层为分类目录。"""
    out: list[str] = []

    def rec(path: str, depth: int):
        for it in c.list_dir(path, force_refresh=False):
            if it["isDir"]:
                if skip_dir(it["name"]) or depth >= max_depth:
                    continue
                out.append(it["path"])
                rec(it["path"], depth + 1)

    rec(root, 1)
    return out


def main():
    ap = argparse.ArgumentParser(description="v2 规则落地审计（只读）")
    ap.add_argument("--root", default=None, help="只审计该目录，默认全部已配置分类")
    args = ap.parse_args()

    c = CD2Client()
    # 默认分类目录可用环境变量覆盖（逗号分隔），便于适配不同媒体库结构
    roots = ([args.root] if args.root else
             [x for x in os.environ.get(
                 "MEDIA_ROOTS", "/media/tv,/media/anime,/media/documentary,/media/movies"
             ).split(",") if x.strip()])

    stat: dict[str, dict] = {}
    for root in roots:
        cat_dirs = [it for it in c.list_dir(root, force_refresh=False) if it["isDir"]]
        # 剧集类多一层分类（国产剧/美剧/...），层数由 MEDIA_TV_ROOT 指定
        tv_root = os.environ.get("MEDIA_TV_ROOT", "/media/tv")
        leaf_dirs: list[str] = []
        for it in cat_dirs:
            leaf_dirs.append(it["path"])
            leaf_dirs += walk_dirs(c, it["path"], max_depth=3)
        seen = set()
        for d in leaf_dirs:
            if d in seen:
                continue
            seen.add(d)
            for f in c.list_dir(d, force_refresh=False):
                if f["isDir"] or not is_media(f["name"]):
                    continue
                n = f["name"]
                # 电影式命名 `片名 (年份) 分辨率.ext` 在任何目录下都算合规
                # （动漫目录里也可能放剧场版）
                if RE_MOVIE_V2.match(n):
                    kind = "v2"
                elif RE_EP_LEGACY.search(n):
                    kind = "legacy"          # 仍带技术信息 → 待改
                elif RE_EP_V2.search(n):
                    kind = "v2"
                else:
                    kind = "legacy"          # 形态不明/孤条字幕 → 计入待改，由人工过目
                # 归到「作品」一级：/media/tv/shows/示例剧集/第1季 → 示例剧集
                parts = d[len(root):].strip("/").split("/")
                if root == tv_root:
                    work = parts[1] if len(parts) > 1 else (parts[0] if parts else d)
                else:
                    work = parts[0] if parts else d
                st = stat.setdefault(f"{root}/{work}", {"v2": 0, "legacy": 0})
                st[kind] += 1

    print(f"{'作品':<44}{'已v2':>7}{'待改':>7}")
    print("-" * 60)
    tv2 = tleg = 0
    for k in sorted(stat):
        st = stat[k]
        tv2 += st["v2"]
        tleg += st["legacy"]
        flag = "  ✔" if st["legacy"] == 0 else ""
        print(f"{k:<44}{st['v2']:>7}{st['legacy']:>7}{flag}")
    print("-" * 60)
    print(f"{'合计':<44}{tv2:>7}{tleg:>7}")
    print(f"\n已符合 v2：{tv2} 个文件；仍需改造：{tleg} 个文件")
    print("（已跳过番外/特典目录：番外/特别篇/SP/Special/OVA/OAD/Extra/Fonts/Scans/menu）")
    c.close()


if __name__ == "__main__":
    main()
