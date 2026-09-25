"""
重命名计划执行器 —— 走 CloudDrive2 gRPC API，不经过 F: 挂载盘。

只支持四种动作：mkdir / rename / move / verify。
绝不删除、绝不上传下载、绝不覆盖同名文件。

用法：
    # 1) 预览（默认，什么都不改）
    python apply_plan.py plans/xxx.json

    # 2) 只跑前 N 条（最小测试）
    python apply_plan.py plans/xxx.json --limit 3

    # 3) 真正执行（二次确认后）
    python apply_plan.py plans/xxx.json --apply

    # 4) 执行后校验（重新列目录，forceRefresh 绕过缓存）
    python apply_plan.py plans/xxx.json --verify

计划文件格式（JSON）：
{
  "name": "Example Show",
  "api_root": "/media/anime/Example-Show",
  "note": "对应挂载盘 F:\\media\\anime\\Example-Show（仅注释，执行不走 F:）",
  "operations": [
    {"op": "mkdir",  "parent": "/media/anime/Example-Show", "name": "第1季"},
    {"op": "rename", "path": "/media/anime/Example-Show/a.mkv", "new_name": "b.mkv"},
    {"op": "move",   "paths": ["/media/anime/Example-Show/a.mkv"], "dest": "/media/anime/Example-Show/第1季"}
  ]
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client, CD2Error, redact  # noqa: E402


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def base_of(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def assert_cloud_paths(plan: dict) -> None:
    """硬性护栏：所有路径必须是 CloudDrive2 云端绝对路径（/ 开头）。

    出现 Windows 盘符（F:\\…）、反斜杠或 UNC 一律直接拒绝——
    从源头保证 rename/move 绝不可能落到 F: 挂载盘上。
    """
    bad = []

    def check(p: str, where: str):
        if not isinstance(p, str):
            return
        if (not p.startswith("/")) or ("\\" in p) or (":" in p) or p.startswith("//"):
            bad.append(f"{where}: {p}")

    for i, op in enumerate(plan.get("operations", [])):
        kind = op.get("op")
        if kind == "mkdir":
            check(op.get("parent"), f"#{i} mkdir.parent")
        elif kind == "rename":
            check(op.get("path"), f"#{i} rename.path")
        elif kind == "move":
            for p in op.get("paths", []):
                check(p, f"#{i} move.paths")
            check(op.get("dest"), f"#{i} move.dest")
    if bad:
        raise SystemExit(
            "计划中存在非云端路径（疑似挂载盘/本地路径），已中止：\n  "
            + "\n  ".join(bad)
        )


class PlanRunner:
    def __init__(self, plan: dict, client: CD2Client):
        self.plan = plan
        self.c = client
        self.ops = plan.get("operations", [])
        self.skipped: list[str] = []
        self.done: list[str] = []

    # ---------- 预检 ----------

    def precheck(self, limit: int | None = None) -> list[tuple[int, dict, str, str]]:
        """返回 [(序号, op, 状态, 说明)]，状态 ∈ READY / SKIPPED_CONFLICT / NEED_REVIEW"""
        rows = []
        planned_targets: set[str] = set()
        for i, op in enumerate(self.ops):
            if limit is not None and i >= limit:
                break
            kind = op["op"]
            if kind == "mkdir":
                parent, name = op["parent"], op["name"]
                if self.c.exists(parent, name):
                    rows.append((i, op, "SKIP(已存在)", f"{parent}/{name}"))
                else:
                    rows.append((i, op, "READY", f"新建 {parent}/{name}"))
            elif kind == "rename":
                path, new_name = op["path"], op["new_name"]
                parent = parent_of(path)
                src = self.c.stat(parent, base_of(path))
                target = f"{parent}/{new_name}"
                if src is None:
                    # 源已不在：若目标已存在，说明这条此前已执行过 → 幂等跳过
                    if self.c.exists(parent, new_name):
                        rows.append((i, op, "SKIP(已完成)", target))
                    else:
                        rows.append((i, op, "NEED_REVIEW", f"源与目标都不存在 {path}"))
                    continue
                if new_name == base_of(path):
                    rows.append((i, op, "SKIP(已合规)", target))
                elif self.c.exists(parent, new_name) or target in planned_targets:
                    rows.append((i, op, "SKIPPED_CONFLICT", f"目标已存在 {target}"))
                else:
                    planned_targets.add(target)
                    rows.append((i, op, "READY", f"{base_of(path)}  →  {new_name}"))
            elif kind == "move":
                dest = op["dest"]
                if not self.c.exists(parent_of(dest), base_of(dest)):
                    rows.append((i, op, "NEED_REVIEW", f"目标目录不存在 {dest}"))
                    continue
                bad = [p for p in op["paths"]
                       if self.c.exists(dest, base_of(p)) or
                       self.c.stat(parent_of(p), base_of(p)) is None]
                if bad:
                    rows.append((i, op, "SKIPPED_CONFLICT", f"冲突/缺失 {bad}"))
                else:
                    rows.append((i, op, "READY",
                                 f"{len(op['paths'])} 项  →  {dest}"))
            else:
                rows.append((i, op, "NEED_REVIEW", f"未知动作 {kind}"))
        return rows

    # ---------- 执行 ----------

    def apply_one(self, op: dict) -> str:
        kind = op["op"]
        if kind == "mkdir":
            self.c.mkdir(op["parent"], op["name"])
            return f"mkdir {op['parent']}/{op['name']}"
        if kind == "rename":
            self.c.rename(op["path"], op["new_name"])
            return f"rename {base_of(op['path'])} -> {op['new_name']}"
        if kind == "move":
            self.c.move(op["paths"], op["dest"], conflict_policy="skip")
            return f"move {len(op['paths'])} -> {op['dest']}"
        raise CD2Error(f"未知动作 {kind}")

    # 网盘限流类错误：errno:12（批量转存出错）/ newno:-9（系统繁忙）/ 超时
    RETRYABLE = ("errno: 12", "errno:12", "newno: -9", "newno:-9", "系统繁忙",
                 "ETIMEDOUT", "DEADLINE_EXCEEDED", "UNAVAILABLE", "RESOURCE_EXHAUSTED")

    def apply_with_retry(self, op: dict, attempts: int = 4) -> str:
        """带退避重试。限流是网盘常态，重试前先等一下，别把批次越打越崩。"""
        last = None
        for k in range(attempts):
            try:
                return self.apply_one(op)
            except Exception as exc:  # noqa: BLE001
                msg = redact(str(exc))
                last = exc
                if not any(s in msg for s in self.RETRYABLE) or k == attempts - 1:
                    raise
                wait = 2 * (k + 1)
                print(f"        …限流/超时，{wait}s 后重试（第 {k+2}/{attempts} 次）")
                time.sleep(wait)
        raise last  # pragma: no cover

    def run(self, limit: int | None = None, assume_yes: bool = False,
            batch: int = 10, delay: float = 1.0) -> dict:
        rows = self.precheck(limit)
        todo = [(i, op) for i, op, st, _ in rows if st == "READY"]
        print(f"\n预检：{len(rows)} 条，其中 READY {len(todo)} 条，"
              f"其余跳过/待复核 {len(rows) - len(todo)} 条")
        if not assume_yes:
            ans = input("确认执行以上 READY 操作？输入 yes 继续：").strip().lower()
            if ans != "yes":
                print("已取消，未做任何修改。")
                return {"applied": 0, "cancelled": True}
        ok = fail = 0
        failed_ops = []
        for n, (i, op) in enumerate(todo, 1):
            try:
                desc = self.apply_with_retry(op)
                ok += 1
                self.done.append(desc)
                print(f"  [{n}/{len(todo)}] OK   {desc}")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                failed_ops.append(op)
                print(f"  [{n}/{len(todo)}] FAIL {op.get('path') or op.get('parent')}"
                      f" : {redact(str(exc))}")
            if batch and n % batch == 0:
                time.sleep(delay)
        print(f"\n执行完毕：成功 {ok}，失败 {fail}")
        if failed_ops:
            print("失败的条目下次重跑本计划即可（工具幂等：已完成的会自动跳过）")
        return {"applied": ok, "failed": fail, "cancelled": False}

    # ---------- 校验 ----------

    def verify(self) -> bool:
        """重新列目录确认结果。must_exist / must_not_exist 可写
        「目录内名字」或「绝对 API 路径」（含 / 即按路径处理）。"""
        root = self.plan["api_root"]
        print(f"\n校验：重新列目录（forceRefresh 绕过 CD2 目录缓存）{root}")
        items = self.c.list_dir(root, force_refresh=True)
        names = {it["name"] for it in items}
        conds = self.plan.get("verify", {})
        all_ok = True

        def present(want: str) -> bool:
            if "/" in want:
                return self.c.stat(parent_of(want), base_of(want)) is not None
            return want in names

        for want in conds.get("must_exist", []):
            ok = present(want)
            print(("  OK   存在 " if ok else "  FAIL 缺失 ") + want)
            all_ok &= ok

        for gone in conds.get("must_not_exist", []):
            ok = not present(gone)
            print(("  OK   旧名已消失 " if ok else "  FAIL 仍存在旧名 ") + gone)
            all_ok &= ok

        want_count = conds.get("expect_file_count")
        if want_count is not None:
            files = [it for it in items if not it["isDir"]]
            ok = len(files) == want_count
            print(f"  {'OK  ' if ok else 'FAIL'} 文件数 = {len(files)}（期望 {want_count}）")
            all_ok &= ok

        print("校验结果：" + ("通过" if all_ok else "存在问题"))
        return all_ok


def main():
    ap = argparse.ArgumentParser(description="CloudDrive2 API 重命名计划执行器")
    ap.add_argument("plan", help="计划 JSON 文件")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="跳过交互式二次确认（配合 --apply）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条（最小测试）")
    ap.add_argument("--batch", type=int, default=10,
                    help="每处理 N 条歇一下，规避网盘限流（默认 10）")
    ap.add_argument("--delay", type=float, default=1.0, help="歇息秒数（默认 1.0）")
    ap.add_argument("--verify", action="store_true", help="执行后重新列目录校验")
    ap.add_argument("--list-only", action="store_true", help="只列当前目录，不做任何预检")
    args = ap.parse_args()

    plan = json.load(open(args.plan, encoding="utf-8"))
    assert_cloud_paths(plan)  # 拒绝任何 F:\ 之类的挂载盘/本地路径
    c = CD2Client()
    runner = PlanRunner(plan, c)
    print(f"计划：{plan.get('name')}   API 根路径：{plan['api_root']}")
    print(f"gRPC 端口：{c.port}（不经 F: 挂载盘）")

    if args.list_only:
        for it in sorted(c.list_dir(plan["api_root"], force_refresh=True),
                         key=lambda x: x["name"]):
            print(("  [D] " if it["isDir"] else "  [F] ") + it["name"])
        c.close()
        return

    rows = runner.precheck(args.limit)
    print("\n{:<5} {:<8} {:<18} {}".format("序号", "动作", "状态", "说明"))
    for i, op, st, msg in rows:
        print("{:<5} {:<8} {:<18} {}".format(i, op["op"], st, msg))

    need = [r for r in rows if r[2] in ("NEED_REVIEW", "SKIPPED_CONFLICT")]
    if need:
        print(f"\n需主人拍板：{len(need)} 条（NEED_REVIEW / SKIPPED_CONFLICT，不会执行）")

    if args.apply:
        runner.run(args.limit, assume_yes=args.yes, batch=args.batch,
                   delay=args.delay)
        if args.verify:
            runner.verify()
    else:
        print("\n[dry-run] 未做任何修改。确认无误后加 --apply 执行。")
    c.close()


if __name__ == "__main__":
    main()
