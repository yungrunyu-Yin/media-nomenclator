"""
按「删除清单」删除文件 —— 本项目里**唯一**允许删除的入口（其余工具一律不含删除）。

★ 允许删除的对象只有两类（其余一律拒绝）：
  A. **字幕文件**：ass / srt / ssa / sub / vtt / smi
  B. **完全空的目录**：递归确认里面既没有文件、也没有子目录（典型场景：单季作品
     多余的「第1季」目录，文件已全部上移后剩下的空壳）
  → **含任何内容的目录、任何视频文件，本脚本一律拒绝删除**（要删视频请另写脚本并单独确认）。

★ 其他安全设计：
  · 路径必须出现在调用方给出的清单 JSON 里，不接受命令行传任意路径。
  · 默认 **dry-run**，什么都不删；必须 `--apply --yes` 才执行。
  · 用普通 `DeleteFile`（云端会进**回收站**，可恢复），**不是**永久删除。
  · 删除后重新列目录复核。

清单 JSON 格式：
{
  "reason": "只保留简体字幕：删除繁体版本",
  "files": [
    {"path": "/media/anime/Example-Show/S01/E01.cht.ass", "why": "繁体，已有简体替代"},
    {"path": "/media/anime/Example-Show/第1季",           "why": "单季作品多余的空季目录"}
  ]
}

用法：
    python delete_list.py plans/deletions.json            # 预览
    python delete_list.py plans/deletions.json --apply --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client, redact  # noqa: E402
import clouddrive_pb2 as pb  # noqa: E402

SUB_EXT = ("ass", "srt", "ssa", "sub", "vtt", "smi")


def dir_is_empty(c: CD2Client, path: str, depth: int = 0) -> tuple[bool, str]:
    """递归确认目录里既无文件也无子目录。返回 (是否空, 说明)。"""
    for it in c.list_dir(path, force_refresh=True):
        if it["isDir"]:
            if depth >= 5:
                return False, f"层级过深，不敢判定：{it['path']}"
            sub_ok, why = dir_is_empty(c, it["path"], depth + 1)
            if not sub_ok:
                return False, why
        else:
            return False, f"含文件 {it['path']}"
    return True, "完全空"


def main():
    ap = argparse.ArgumentParser(description="按清单删除字幕 / 空目录（走云端回收站）")
    ap.add_argument("list_json", help="删除清单 JSON")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    data = json.load(open(args.list_json, encoding="utf-8"))
    files = data.get("files", [])
    print(f"清单：{args.list_json}")
    print(f"事由：{data.get('reason', '(未注明)')}")
    print(f"条目：{len(files)} 个\n")

    c = CD2Client()
    print(f"CloudDrive2 gRPC：127.0.0.1:{c.port}（不经 F: 挂载盘）\n")

    # ---- 逐条判定：只接受「字幕文件」或「完全空的目录」----
    todo, missing, refused = [], [], []
    for f in files:
        path = f["path"]
        parent, name = path.rsplit("/", 1)
        if not c.exists(parent, name):
            missing.append(path)
            continue
        info = c.stat(parent, name)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if info and info["isDir"]:
            ok, why = dir_is_empty(c, path)
            if ok:
                todo.append({**f, "kind": "空目录", "note": why})
            else:
                refused.append((path, f"目录非空（{why}）"))
        elif ext in SUB_EXT:
            todo.append({**f, "kind": "字幕", "note": ""})
        else:
            refused.append((path, f"不是字幕也不是目录（扩展名 .{ext}）"))

    print(f"可删：{len(todo)} 个；已不存在（跳过）：{len(missing)} 个；拒绝：{len(refused)} 个")
    for f in todo:
        tag = f"[{f['kind']}]"
        print(f"  {tag} {f['path']}\n        事由：{f.get('why', '')}" +
              (f"  ·  {f['note']}" if f["note"] else ""))
    for p in missing:
        print(f"  [跳过] {p}")
    for p, why in refused:
        print(f"  [拒绝] {p}  →  {why}")

    if refused:
        print("\n⚠ 有被拒绝的条目（本脚本拒绝删除含内容的目录与视频）。请从清单中移除后再执行。")
        c.close()
        sys.exit(2)
    if not todo:
        print("\n无可删除项。")
        c.close()
        return

    n_sub = sum(1 for f in todo if f["kind"] == "字幕")
    n_dir = sum(1 for f in todo if f["kind"] == "空目录")
    if not args.apply:
        print(f"\n[dry-run] 未删除任何东西。确认无误后加 --apply --yes。"
              f"（字幕 {n_sub} 个、空目录 {n_dir} 个，走云端回收站可恢复）")
        c.close()
        return
    if not args.yes:
        if input(f"确认删除以上 {len(todo)} 项？输入 yes 继续：").strip().lower() != "yes":
            print("已取消。")
            c.close()
            return

    ok = fail = 0
    for f in todo:
        try:
            r = c.stub.DeleteFile(pb.FileRequest(path=f["path"]),
                                  timeout=c.timeout, metadata=c._md)
            if r.success:
                ok += 1
                print(f"  OK   [{f['kind']}] {f['path']}")
            else:
                fail += 1
                print(f"  FAIL {f['path']} -> {r.errorMessage}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {f['path']} -> {redact(str(exc))}")

    print(f"\n删除结果：成功 {ok}，失败 {fail}")
    print("\n复核（重新列目录）：")
    left = 0
    for parent in sorted({f["path"].rsplit("/", 1)[0] for f in todo}):
        names = {x["name"] for x in c.list_dir(parent, force_refresh=True)}
        for f in todo:
            if f["path"].rsplit("/", 1)[0] == parent and f["path"].rsplit("/", 1)[-1] in names:
                print("   仍存在:", f["path"])
                left += 1
    print(f"   残留：{left} 个" + (" ✓" if left == 0 else " ⚠"))
    c.close()
    sys.exit(0 if (fail == 0 and left == 0) else 1)


if __name__ == "__main__":
    main()
