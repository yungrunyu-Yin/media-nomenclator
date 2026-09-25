"""
规则 v2 批量转换：生成重命名计划（纯生成，不碰文件）。

★ 本脚本**只做机械改造**，不重新查证任何标题：
    剧名.年份.SxxExx.集标题.<技术信息…>.ext
      →  剧名.年份.SxxExx.「集标题」.ext
    若本就没有集标题：剧名.年份.SxxExx.<技术信息…>.ext  →  剧名.年份.SxxExx.ext

安全设计（宁可标 NEED_REVIEW 也不猜）：
  1. 技术信息块必须**能被完整识别**（以「分辨率令牌」为锚点，向左向右都只含技术令牌）
     才动手；只要有一个令牌认不出来，整条标 NEED_REVIEW，不改。
  2. 已符合 v2 的文件（集标题已带「」且其后无内容）直接跳过。
  3. 番外/特别篇/SP/Special/OVA/OAD/Extra/Fonts/Scans/menu 目录整体不动。
  4. 处理前先 stat 目标名，已存在则标 SKIPPED_CONFLICT，绝不覆盖。

生成的计划交给 `apply_plan.py` 执行（默认 dry-run）：
    python v2_plan.py --root /media/tv/shows --out plans/tv_v2.json
    python apply_plan.py plans/tv_v2.json
    python apply_plan.py plans/tv_v2.json --apply --yes --verify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client  # noqa: E402

MEDIA_EXT = ("mkv", "mp4", "avi", "mov", "ts")
SUB_EXT = ("ass", "srt", "ssa", "sub", "vtt", "smi")
SKIP_DIR_WORDS = ("番外", "特别篇", "特别集", "花絮", "幕后", "Special",
                  "Specials", "OVA", "OAD", "Extra", "Fonts", "Scans", "menu")

# ---- 技术令牌识别 ----
# ★ 复用 scripts/techinfo_reorder.py 的保护技巧：声道 5.1 / 7.1 内含点号，
#   而字段分隔符也是点号；不先保护就会被切成 "5" 和 "1" 而无法归类。
#   （该脚本自测抓到的真实 bug，原样沿用其正则）
# ★ 同类坑：编码 H.264 / H.265 内部也含点号，会被切成 "H" + "265"。
#   自测抓到后补上第二道保护。
_CH = re.compile(r"(?<!\d)(\d)\.(\d)(?!\d)")
_CODEC_DOT = re.compile(r"\bH\.(\d{3})\b", re.I)
_SENTINEL = "\x00"

TECH_VOCAB = {
    # 来源
    "web-dl", "webdl", "web", "webrip", "bluray", "blu-ray", "bdrip", "brrip",
    "remux", "hdtv", "dvd", "dvdrip", "hdrip", "uhd", "bd", "amzn", "nf", "cr",
    "dsnp", "hmax", "atvp", "itunes", "baha", "b-global", "bilibili", "youku",
    "tencent", "mgtv", "iqiyi", "iq", "viu", "viki", "wetv", "kocowa", "abema",
    "tver", "wowow", "fod", "u-next", "hulu", "atv", "hbo", "paramount",
    "peacock", "appletv", "disney", "cntv", "cctv", "mytvsuper",
    # 动态范围
    "sdr", "hdr", "hdr10", "hdr10+", "hdr10plus", "dovi", "dv", "hlg", "edr",
    "dolby", "vision",
    # 编码
    "avc", "hevc", "av1", "vp9", "mpeg2",
    # 位深
    "8bit", "10bit", "12bit",
    # 音频
    "aac", "ac3", "eac3", "dd", "dd+", "ddp", "dts", "dts-hd", "dts-hd.ma",
    "truehd", "atmos", "flac", "mp3", "pcm", "lpcm", "opus", "multi", "dual",
    "ma",
    # 杂项技术/封装标记
    "内封字幕", "年糕封装", "vostfr", "repack", "proper", "2audio", "3audio",
}
RE_TECH_TOKEN = re.compile(
    r"^(?:(?:HD|FHD|UHD|SD)?\d{3,4}[pi]|[48][kK]|\d{1,2}bit|[1-8]\.\d|\d+audio"
    r"|h\.?26[45]|[xX]26[45]|dd\+?p?|ddp\d?\.?\d?|dts-hd\.ma)$", re.I)
# 语言/来源标记（字幕常见，按 Skill §10.2 去掉）
LANG_TOKENS = {
    "zh", "zh-cn", "zh-tw", "chs", "cht", "sc", "tc", "tcjp", "chs&eng",
    "en", "eng", "ja", "jp", "jpn", "ko", "kor", "简体", "繁体", "中字",
    "双语", "中英", "人人双语",
}
RE_RESOLUTION = re.compile(
    r"^(?:(?:HD|FHD|UHD|SD)?\d{3,4}[pi]|[48][kK])$", re.I)
RE_EPISODE = re.compile(
    r"^(?P<head>.+?\.S\d{2}E\d{2}(?:-E\d{2})?)(?:\.(?P<rest>.*))?$", re.I)
# 疑似含技术信息（用于「不敢判断」时兜底提示）
RE_LOOKS_TECH = re.compile(
    r"\.(?:\d{3,4}p|web-dl|webrip|bluray|bdrip|remux|hdtv|hevc|h\.?26[45]|x26[45]|aac|ddp|flac)\b",
    re.I)


def is_tech(tok: str) -> bool:
    """单个令牌是否属于技术信息。保守：明确词表项，或严格样式的数字型令牌。"""
    low = tok.lower()
    if low in TECH_VOCAB:
        return True
    return bool(RE_TECH_TOKEN.match(tok))


def tokenize(rest: str) -> list[str]:
    """按点号切词，但先保护含点号的技术令牌（5.1 / H.265），避免被切碎。"""
    protected = _CH.sub(r"\1" + _SENTINEL + r"\2", rest)
    protected = _CODEC_DOT.sub("H" + _SENTINEL + r"\1", protected)
    return [t.replace(_SENTINEL, ".") for t in protected.split(".") if t]


def split_title_tech(tokens: list[str]) -> tuple[list[str], list[str], str]:
    """把「集标题令牌」与「技术令牌」切开。返回 (title, tech, status)。"""
    if not tokens:
        return [], [], "ok"
    res_idx = [i for i, t in enumerate(tokens) if RE_RESOLUTION.match(t)]
    if not res_idx:
        # 没有分辨率令牌：若还混着技术令牌 → 不敢判断
        if any(is_tech(t) for t in tokens):
            return tokens, [], "ambiguous"
        return tokens, [], "no_tech"
    r = res_idx[-1]
    # 锚点左右两侧必须全是技术令牌，否则不猜
    if not all(is_tech(t) for t in tokens[r + 1:]):
        return tokens, [], "ambiguous"
    j = r
    while j - 1 >= 0 and is_tech(tokens[j - 1]):
        j -= 1
    return tokens[:j], tokens[j:], "ok"


def convert(name: str) -> tuple[str | None, str]:
    """返回 (新名, 原因)。新名为 None 表示不改。"""
    if "." not in name:
        return None, "NEED_REVIEW(无扩展名)"
    ext = name.rsplit(".", 1)[-1]
    low_ext = ext.lower()
    if low_ext not in MEDIA_EXT + SUB_EXT:
        return None, "SKIP(非媒体文件)"
    stem = name[: -(len(ext) + 1)]

    m = RE_EPISODE.match(stem)
    if not m:
        return None, "SKIP(不符合 剧名.年份.SxxExx.… 形态)"
    head = m.group("head")
    rest = m.group("rest") or ""

    # 已经是 v2（集标题带「」且其后无内容）→ ⚠️ 注意：「」内含点号时按点切分会误判，
    # 所以这里整体用正则判断，不走切分逻辑
    if re.match(r"^「.+」$", rest) or rest == "":
        return None, "SKIP(已符合 v2)"

    tokens = tokenize(rest)
    if not tokens:
        return None, "SKIP(已符合 v2)"

    # 去掉语言标记（字幕常见，按 §10.2）
    dedup_lang = [t for t in tokens if t.lower() not in LANG_TOKENS]
    if len(dedup_lang) != len(tokens):
        tokens = dedup_lang

    title_tokens, tech_tokens, status = split_title_tech(tokens)
    if status == "ambiguous":
        return None, "NEED_REVIEW(技术块无法可靠切分)"

    if title_tokens:
        title = ".".join(title_tokens)
        new = f"{head}.「{title}」.{ext}"
    else:
        new = f"{head}.{ext}"
    if new == name:
        return None, "SKIP(已符合 v2)"
    return new, ("改成 无集标题 + 无技术信息" if not title_tokens
                 else ("删除技术信息 " + ".".join(tech_tokens) if tech_tokens
                       else "仅给集标题加「」"))


def collect_files(c: CD2Client, root: str, max_depth: int = 3) -> list[dict]:
    out: list[dict] = []

    def rec(path: str, depth: int):
        for it in c.list_dir(path, force_refresh=False):
            if it["isDir"]:
                low = it["name"].lower()
                if any(w.lower() in low for w in SKIP_DIR_WORDS) or depth >= max_depth:
                    continue
                rec(it["path"], depth + 1)
            else:
                out.append(it)

    rec(root, 0)
    return out


def main():
    ap = argparse.ArgumentParser(description="规则 v2 批量转换计划生成（只读）")
    ap.add_argument("--root", required=True, help="要转换的目录，如 /media/tv/shows")
    ap.add_argument("--out", required=True, help="计划 JSON 输出路径")
    args = ap.parse_args()

    # Git Bash 会把 /media/... 这类参数改写成 Windows 路径（MSYS 路径转换）
    if ":" in args.root or "\\" in args.root:
        raise SystemExit(
            f"路径被 shell 改写了：{args.root}\n"
            "请加环境变量 MSYS_NO_PATHCONV=1 重跑，例如：\n"
            f"  MSYS_NO_PATHCONV=1 python v2_plan.py --root {args.root} --out ..."
        )

    c = CD2Client()
    files = collect_files(c, args.root)
    ops, skipped, review = [], [], []
    names = {f["name"] for f in files}

    for f in files:
        new, why = convert(f["name"])
        if new is None:
            (review if why.startswith("NEED_REVIEW") else skipped).append(
                (f["path"], why))
            continue
        parent = f["path"].rsplit("/", 1)[0]
        # 目标已存在（且不是自己）→ 冲突，不覆盖
        if new in names and new != f["name"]:
            review.append((f["path"], f"SKIPPED_CONFLICT(目标已存在 {new})"))
            continue
        ops.append({"op": "rename", "path": f["path"], "new_name": new})

    plan = {
        "name": f"{args.root} → 规则 v2",
        "api_root": args.root,
        "note": "纯机械改造：删技术信息 + 集标题加「」。不重查标题。走 CloudDrive2 gRPC，不经 F:",
        "operations": ops,
        "verify": {
            "must_exist": [f"{op['path'].rsplit('/',1)[0]}/{op['new_name']}"
                           for op in ops[:2] + ops[-1:]] if ops else [],
            "must_not_exist": [op["path"] for op in ops[:2] + ops[-1:]] if ops else [],
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)

    print(f"扫描 {args.root}：媒体文件 {len(files)} 个")
    print(f"  待改名 READY   ：{len(ops)}")
    print(f"  无需改动 SKIP  ：{len(skipped)}")
    print(f"  需人工确认     ：{len(review)}")
    if review:
        print("\n  需人工确认的条目：")
        for p, w in review[:30]:
            print(f"    {w}  {p}")
        if len(review) > 30:
            print(f"    … 其余 {len(review)-30} 条同类")
    print(f"\n计划已写出：{args.out}")
    c.close()


if __name__ == "__main__":
    main()
