"""
一次性清理：删除 API 自检产生的空测试目录。

★ 这是本项目里**唯一**允许删除的场景，且做了三重硬保险：
  1. 目标名必须在下方 ALLOWLIST 里（白名单写死，不接受命令行传入任意路径）；
  2. 目标父目录必须恰好是 BASE；
  3. 删除前递归确认目录内**一个文件都没有**，只要发现文件立即中止。

用的是 CD2 的 `DeleteFile`（普通删除，云端会进回收站，可恢复），
**不是** `DeleteFilePermanently`。共享客户端 `cd2_client.py` 仍然不提供删除能力，
通用执行器 `apply_plan.py` 也仍然只支持 mkdir / rename / move。

用法：
    python cleanup_testdirs.py            # 预览（默认，不删）
    python cleanup_testdirs.py --apply    # 真正删除
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client, CD2Error, redact  # noqa: E402
import clouddrive_pb2 as pb  # noqa: E402

# 自检测试目录的父目录：与 selftest_api.py 的 CD2_SELFTEST_BASE 保持一致
BASE = os.environ.get("CD2_SELFTEST_BASE", "/")
ALLOWLIST = ("__cd2_api_selftest__", "__cd2_api_selftest_renamed__")


def collect(c: CD2Client, path: str, depth: int = 0) -> list[dict]:
    acc: list[dict] = []
    for it in c.list_dir(path, force_refresh=True):
        acc.append(it)
        if it["isDir"] and depth < 5:
            acc += collect(c, it["path"], depth + 1)
    return acc


def main():
    ap = argparse.ArgumentParser(description="删除 API 自检产生的空测试目录（限白名单）")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    c = CD2Client()
    print(f"CloudDrive2 gRPC 已连接：127.0.0.1:{c.port}")

    # ---- 保险 1 & 2：白名单 + 父目录校验 ----
    targets: list[str] = []
    for name in ALLOWLIST:
        if "/" in name or "\\" in name or ":" in name:
            raise SystemExit(f"白名单项非法：{name}")
        p = f"{BASE}/{name}"
        if c.exists(BASE, name):
            targets.append(p)
    print(f"白名单内实际存在的目标：{targets or '无（都已不在，无需清理）'}")
    if not targets:
        c.close()
        return

    # ---- 保险 3：递归确认零文件 ----
    plan: list[str] = []
    for t in targets:
        items = collect(c, t)
        files = [x for x in items if not x["isDir"]]
        dirs = [x for x in items if x["isDir"]]
        print(f"\n{t}")
        print(f"  子目录 {len(dirs)} 个，文件 {len(files)} 个")
        if files:
            c.close()
            raise SystemExit(
                "⚠ 目录内发现文件，已中止！请主人确认：\n  "
                + "\n  ".join(f["path"] for f in files)
            )
        # 深到浅：先删子目录，再删父目录
        plan += [d["path"] for d in sorted(dirs, key=lambda x: -x["path"].count("/"))]
        plan.append(t)
        for d in dirs:
            print("    [D]", d["path"])

    print(f"\n将删除 {len(plan)} 个空目录（普通删除，云端回收站可恢复）：")
    for p in plan:
        print("   ", p)

    if not args.apply:
        print("\n[dry-run] 未删除任何东西。确认无误后加 --apply。")
        c.close()
        return
    if not args.yes:
        if input("确认删除以上空目录？输入 yes 继续：").strip().lower() != "yes":
            print("已取消。")
            c.close()
            return

    ok = fail = 0
    for p in plan:
        try:
            r = c.stub.DeleteFile(pb.FileRequest(path=p), timeout=c.timeout,
                                  metadata=c._md)
            if r.success:
                ok += 1
                print(f"  OK   {p}")
            else:
                fail += 1
                print(f"  FAIL {p} -> {r.errorMessage}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {p} -> {redact(str(exc))}")

    print(f"\n删除结果：成功 {ok}，失败 {fail}")
    print("\n复核（重新列目录）：")
    left = [it["name"] for it in c.list_dir(BASE, force_refresh=True)
            if it["name"] in ALLOWLIST]
    print("  残留测试目录：" + (", ".join(left) if left else "无 ✓"))
    c.close()
    sys.exit(0 if (fail == 0 and not left) else 1)


if __name__ == "__main__":
    main()
