"""
只读探测：列出 MKV/WebM 容器内的轨道（视频/音频/**字幕**）。

用途：判断一个视频是否**内封字幕**（字幕轨封在容器里），
      而不是靠「目录里有没有 .ass/.srt」来猜。

实现：极简 EBML(MKV) 解析，只读文件前若干 MB 的头部（Tracks 元素），
      **不读取也不下载视频正文**。零第三方依赖。

用法：
    python probe_tracks.py "F:/media/tv/shows/Example-Show/S01/xxx.mkv"
    python probe_tracks.py <file> [file2 ...]
"""

from __future__ import annotations

import os
import sys

TRACK_TYPE = {1: "视频", 2: "音频", 17: "字幕", 18: "字幕(按钮)"}


class Ebml:
    def __init__(self, fh, limit=48 * 1024 * 1024):
        self.fh = fh
        self.limit = limit
        self.pos = 0

    def _read(self, n: int) -> bytes:
        if self.pos >= self.limit:
            raise EOFError("超出探测上限")
        b = self.fh.read(n)
        self.pos += len(b)
        if not b:
            raise EOFError
        return b

    def vint(self, keep_marker: bool) -> tuple[int, int]:
        """返回 (值, 字节数)。keep_marker=True 时保留长度标记（EBML ID 用）。"""
        first = self._read(1)[0]
        if first == 0:
            raise ValueError("非法 VINT")
        length = 1
        mask = 0x80
        while not (first & mask):
            mask >>= 1
            length += 1
        val = first if keep_marker else (first & (mask - 1))
        for _ in range(length - 1):
            val = (val << 8) | self._read(1)[0]
        return val, length

    def skip(self, n: int):
        if n <= 0:
            return
        if self.pos + n > self.limit:
            raise EOFError("超出探测上限")
        self.fh.seek(n, os.SEEK_CUR)
        self.pos += n

    def uint(self, size: int) -> int:
        return int.from_bytes(self._read(size), "big")

    def string(self, size: int) -> str:
        return self._read(size).decode("utf-8", "replace").strip("\x00")


def split_ids(raw: bytes) -> list[bytes]:
    """把可能存多个 ID 的字符串切开（如 '3\\\\0eng\\\\0'）。"""
    return [p for p in raw.split(b"\x00") if p]


def probe(path: str) -> dict:
    """返回 {'tracks': [ {type,codec,lang,name} ], 'ok': bool, 'err': str}"""
    tracks: list[dict] = []
    try:
        with open(path, "rb") as fh:
            eb = Ebml(fh)
            # 顶层：EBML 头 + Segment
            depth = 0
            while True:
                eid, _ = eb.vint(True)
                size, _ = eb.vint(False)
                if eid == 0x18538067:            # Segment → 进去找 Tracks
                    depth = 1
                    unknown = (1 << (8 * 0)) - 1     # 占位
                    continue
                if depth == 1 and eid == 0x1654AE6B:  # Tracks
                    end = eb.pos + size
                    while eb.pos < end:
                        tid, _ = eb.vint(True)
                        tsz, _ = eb.vint(False)
                        if tid == 0xAE:               # TrackEntry
                            tend = eb.pos + tsz
                            t = {"type": None, "codec": "", "lang": "", "name": ""}
                            while eb.pos < tend:
                                sid, _ = eb.vint(True)
                                ssz, _ = eb.vint(False)
                                if sid == 0x83:
                                    t["type"] = eb.uint(ssz)
                                elif sid == 0x86:
                                    t["codec"] = eb.string(ssz)
                                elif sid == 0x22B59C:
                                    t["lang"] = eb.string(ssz)
                                elif sid == 0x536E:
                                    t["name"] = eb.string(ssz)
                                else:
                                    eb.skip(ssz)
                            tracks.append(t)
                        else:
                            eb.skip(tsz)
                    break
                # 其它顶层元素：按大小跳过（Segment 的 size 常为未知，走到这里就按需读）
                if size == (1 << (7 * 8)) - 1 or size < 0:
                    continue
                try:
                    eb.skip(size)
                except EOFError:
                    break
        return {"ok": True, "tracks": tracks, "err": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tracks": tracks, "err": f"{type(exc).__name__}: {exc}"}


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for path in sys.argv[1:]:
        r = probe(path)
        print(f"\n{path}")
        if not r["ok"] and not r["tracks"]:
            print("   探测失败：", r["err"])
            continue
        subs = [t for t in r["tracks"] if t["type"] in (17, 18)]
        print(f"   轨道 {len(r['tracks'])} 条（视频 "
              f"{sum(1 for t in r['tracks'] if t['type'] == 1)}，音频 "
              f"{sum(1 for t in r['tracks'] if t['type'] == 2)}，"
              f"字幕 {len(subs)}）")
        for t in r["tracks"]:
            kind = TRACK_TYPE.get(t["type"], f"?{t['type']}")
            extra = " ".join(x for x in (t["lang"], t["name"]) if x)
            print(f"     - {kind:<6} {t['codec']:<22} {extra}")
        if not r["ok"]:
            print("   （解析提前结束：", r["err"], "）")


if __name__ == "__main__":
    main()
