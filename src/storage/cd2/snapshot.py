"""
目录快照 / 内容完整性比对（只读，不修改任何东西）。

用途：回答「这次整理到底只是改了名字，还是动了文件内容？」这类问题 ——
把某目录下每个文件的 `名字 + 字节数 + MD5` 存成 JSON 快照，
之后随时比对：名字可不同（改名了），但 **size 与 MD5 必须完全一致**。

用法：
    python snapshot.py /media/anime/Example-Show --save baselines/example.json
    python snapshot.py /media/anime/Example-Show --diff baselines/example.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client  # noqa: E402
import clouddrive_pb2 as pb  # noqa: E402

HASH_NAME = {0: "unknown", 1: "md5", 2: "sha1", 3: "pikpak-sha1"}


def collect(c: CD2Client, root: str, max_depth: int = 8) -> list[dict]:
    out: list[dict] = []

    def walk(path: str, depth: int):
        for it in c.list_dir(path, force_refresh=True):
            if it["isDir"]:
                if depth < max_depth:
                    walk(it["path"], depth + 1)
                continue
            f = c.stub.FindFileByPath(
                pb.FindFileByPathRequest(parentPath=path, path=it["name"]),
                timeout=c.timeout, metadata=c._md)
            hashes = {HASH_NAME.get(k, str(k)): v for k, v in f.fileHashes.items()}
            out.append({"name": it["name"], "rel": it["path"][len(root):].lstrip("/"),
                        "size": f.size, "hashes": hashes})

    walk(root, 0)
    return sorted(out, key=lambda x: x["rel"])


def main():
    ap = argparse.ArgumentParser(description="目录快照 / 内容完整性比对（只读）")
    ap.add_argument("root", nargs="?", help="云端 API 路径，如 /media/anime/Example-Show")
    ap.add_argument("--plan", help="改从计划 JSON 的 api_root 取路径（可绕开 Git Bash 路径转换）")
    ap.add_argument("--save", help="保存快照到该 JSON")
    ap.add_argument("--diff", help="与既有快照比对")
    args = ap.parse_args()

    root = args.root
    if args.plan:
        root = json.load(open(args.plan, encoding="utf-8"))["api_root"]
    if not root:
        raise SystemExit("需要给出 root，或用 --plan 指定计划文件")
    # Git Bash 会把 /media/... 这类参数改写成 Windows 路径（MSYS 路径转换）
    if ":" in root or "\\" in root:
        raise SystemExit(
            f"路径被 shell 改写了：{root}\n"
            "请改用 --plan 指定计划文件，或加环境变量 MSYS_NO_PATHCONV=1 重跑。"
        )

    c = CD2Client()
    cur = collect(c, root)
    c.close()
    print(f"{root}：{len(cur)} 个文件")

    if args.save:
        os.makedirs(os.path.dirname(os.path.abspath(args.save)), exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"root": args.root, "files": cur}, fh,
                      ensure_ascii=False, indent=2)
        print(f"快照已保存：{args.save}")

    if args.diff:
        old = json.load(open(args.diff, encoding="utf-8"))["files"]
        old_by_rel = {x["rel"]: x for x in old}
        cur_by_rel = {x["rel"]: x for x in cur}
        same = [r for r in old_by_rel if r in cur_by_rel
                and old_by_rel[r]["size"] == cur_by_rel[r]["size"]
                and old_by_rel[r]["hashes"] == cur_by_rel[r]["hashes"]]
        renamed = [r for r in old_by_rel if r not in cur_by_rel]
        added = [r for r in cur_by_rel if r not in old_by_rel]
        print(f"\n未变（名字与内容都一致）：{len(same)}")
        print(f"名字已变 / 缺失：{len(renamed)}")
        for r in renamed:
            print("  旧:", r)
        print(f"新增：{len(added)}")
        for r in added:
            print("  新:", r)

        # 内容完整性：按 size+md5 配对，确认「改名」而不是「换文件」
        def sig(x):
            return (x["size"], tuple(sorted(x["hashes"].items())))

        old_sigs = sorted(sig(x) for x in old)
        cur_sigs = sorted(sig(x) for x in cur)
        if old_sigs == cur_sigs:
            print("\n★ 内容完整性：所有文件的 字节数 + MD5 多重集完全一致 → "
                  "**只改了名，文件内容一个字节都没动** ✓")
        else:
            print("\n⚠ 内容完整性：存在字节数/哈希不一致的文件，需人工核查！")
            sys.exit(1)


if __name__ == "__main__":
    main()
