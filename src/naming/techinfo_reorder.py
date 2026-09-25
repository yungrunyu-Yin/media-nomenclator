# -*- coding: utf-8 -*-
"""影视文件名「技术信息字段」重排工具（可复用）。

按 naming-config.json 的 fieldOrder 重排：
    来源.分辨率.动态范围.编码.位深.音频.声道

用法:
    from techinfo_reorder import reorder
    reorder("BDRIP.1080P.AC3.H264.10bit")                       -> "BDRIP.1080P.H.264.10bit.AC3"
    reorder("Bdrip.1080P.dts.h264")                             -> "Bdrip.1080P.H.264.dts"
    reorder("UHD Blu-ray 2160p 10bit DoVi 2Audio TrueHD Atmos 7.1 x265")
            -> "UHD.Blu-ray.2160p.DoVi.H.265.10bit.TrueHD.Atmos.7.1"

返回 (新串, 被丢弃的 token 列表)。无法归类的 token（发布标记如 2Audio、发布组如 -GROUP、
字幕/推广后缀等）一律丢弃——丢弃项要人工确认一次，避免误删真实技术字段。

★ 踩过的坑（务必保留保护逻辑）:
  频道号 5.1 / 7.1 内部含点号，而字段分隔符也是点号。若不先保护，
  "7.1" 会被切成 "7" 和 "1" 两个无法识别的 token 而整段丢失（曾导致
  S08 全部 6 个文件失去声道信息）。
"""
import re

ORDER = ["source", "resolution", "dynrange", "codec", "bitdepth", "audioGroup"]

_SRC = {"bdrip", "bd", "bluray", "blu-ray", "brrip", "uhd", "web-dl", "webrip",
        "web", "hdtv", "dvd", "remux", "nf", "amzn", "dsnp", "hmax", "itunes"}
_CODEC = {"h264": "H.264", "x264": "H.264", "avc": "H.264",
          "h265": "H.265", "x265": "H.265", "hevc": "H.265",
          "av1": "AV1", "vp9": "VP9"}
_DYN = {"hdr": "HDR", "hdr10": "HDR10", "hdr10+": "HDR10+", "dovi": "DoVi",
        "dv": "DV", "dolbyvision": "Dolby.Vision", "hlg": "HLG", "sdr": "SDR"}
_AUDIO = {"dts": "DTS", "dts-hd": "DTS-HD", "ac3": "AC3", "eac3": "EAC3",
          "dd": "DD", "ddp": "DDP", "dd+": "DDP", "truehd": "TrueHD",
          "atmos": "Atmos", "flac": "FLAC", "aac": "AAC", "lpcm": "LPCM", "mp3": "MP3"}

# ★ 频道号（5.1 / 7.1）里的点号必须先保护，否则会被当字段分隔符切开而丢失。
#   但只保护「独立的 1~2 位数字.数字」—— 否则 "H264.10bit" 里的 "4.1" 也会被误保护，
#   导致整个 token 无法归类而被丢弃（自测抓到的真实 bug）。
_CH = re.compile(r"(?<!\d)(\d)\.(\d)(?!\d)")
_SENTINEL = "\x00"


def reorder(tech):
    # 保护时务必保留两侧数字：5.1 → 5<NUL>1，最后再还原。
    # 若直接把整个 "5.1" 替换成占位符，数字会一起丢失（自测抓到的真实 bug）。
    protected = _CH.sub(r"\1" + _SENTINEL + r"\2", tech)
    buckets = {k: [] for k in ORDER}
    dropped = []
    for tok in re.split(r"[.\s_]+", protected.strip()):
        if not tok:
            continue
        tok = tok.replace(_SENTINEL, ".")
        low = tok.lower()
        if low in _SRC:
            buckets["source"].append(tok)
        elif re.fullmatch(r"\d{3,4}[piPI]", tok):
            buckets["resolution"].append(tok)
        elif low in _DYN:
            buckets["dynrange"].append(_DYN[low])
        elif low in _CODEC:
            buckets["codec"].append(_CODEC[low])
        elif re.fullmatch(r"\d{1,2}bit", tok, re.I):
            buckets["bitdepth"].append(tok)
        elif low in _AUDIO:
            buckets["audioGroup"].append(tok)
        elif re.fullmatch(r"\d\.\d", tok):
            # 声道与音频同属一个「音频组」，保持原有相对顺序
            # （用户已定稿的写法：DDP.5.1.Atmos / TrueHD.Atmos.7.1）
            buckets["audioGroup"].append(tok)
        else:
            dropped.append(tok)
    out = []
    for k in ORDER:
        out.extend(buckets[k])
    return ".".join(out), dropped


if __name__ == "__main__":
    cases = [
        ("BDRIP.1080P.AC3.H264.10bit", "BDRIP.1080P.H.264.10bit.AC3"),
        ("Bdrip.1080P.dts.h264", "Bdrip.1080P.H.264.dts"),
        ("2160p.NF.WEB-DL.H265.DDP.5.1.Atmos", "NF.WEB-DL.2160p.H.265.DDP.5.1.Atmos"),
        ("UHD Blu-ray 2160p 10bit DoVi 2Audio TrueHD Atmos 7.1 x265",
         "UHD.Blu-ray.2160p.DoVi.H.265.10bit.TrueHD.Atmos.7.1"),
    ]
    ok = True
    for src, want in cases:
        got, dropped = reorder(src)
        flag = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print("%s  %-52s -> %s   dropped=%s" % (flag, src, got, dropped))
    print("ALL_OK" if ok else "HAS_FAILURE")
