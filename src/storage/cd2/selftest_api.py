"""
CloudDrive2 API 最小自检 —— 验证 list / stat / mkdir / rename / move 全链路可用。

安全约束：只创建 / 改名 / 移动，**不删除任何东西**。
因此会在测试父目录下留下一个空的测试目录（__cd2_api_selftest__/），
需要清理时请用同目录的 cleanup_testdirs.py（限白名单、走回收站）。

测试父目录默认取云端根目录 `/`，可用环境变量覆盖：
    CD2_SELFTEST_BASE=/media/anime
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cd2_client import CD2Client, CD2Error, redact  # noqa: E402

BASE = os.environ.get("CD2_SELFTEST_BASE", "/")
A1 = "__cd2_api_selftest__"    # 唯一的测试目录（mkdir 目标）
A1_ALT = "__cd2_api_selftest_renamed__"  # 早期版本自检留下的旧目录，仅用于识别
SUB = "sub"                    # 测试用子目录：rename / move 都在它身上做，且立刻还原
# stat 步骤的取样目录：随便找一个真实存在的作品目录即可，不写死具体文件名
# （写死文件名会在该作品改名后误报失败 —— 已踩过）
SAMPLE_DIRS = tuple(d for d in (os.environ.get("CD2_SELFTEST_SAMPLE"), BASE) if d)


def step(name):
    def deco(fn):
        def run(*a, **kw):
            try:
                r = fn(*a, **kw)
                print(f"  PASS  {name}" + (f"  ({r})" if r else ""))
                return True
            except Exception as exc:  # noqa: BLE001
                detail = redact(str(exc)) or type(exc).__name__
                print(f"  FAIL  {name}  ->  {detail}")
                return False
        return run
    return deco


def main():
    c = CD2Client()
    print(f"CloudDrive2 gRPC 已连接：127.0.0.1:{c.port}（绕开 F: 挂载盘）")
    print("system:", c.system_info())
    print("\n步骤：")

    results = []

    @step(f"list 列目录 {BASE}")
    def t1():
        n = len(c.list_dir(BASE))
        assert n > 0
        return f"{n} 项"

    @step("stat 读取已知文件信息（取样一个真实文件，不写死名字）")
    def t2():
        for d in SAMPLE_DIRS:
            items = c.list_dir(d, force_refresh=True)
            f = next((x for x in items if not x["isDir"]), None)
            if f:
                s = c.stat(d, f["name"])
                assert s and not s["isDir"], f"stat 返回异常 {s}"
                return f"{f['name']} → {s['size']} bytes"
        raise AssertionError("取样目录里没有文件可读")

    @step(f"mkdir 建目录 {BASE}/{A1}")
    def t3():
        if c.exists(BASE, A1):
            return "已存在，跳过创建"
        c.mkdir(BASE, A1)
        assert c.exists(BASE, A1)
        return "创建并可查到"

    @step(f"mkdir 建子目录 {A1}/{SUB}")
    def t4():
        if c.exists(f"{BASE}/{A1}", SUB):
            return "已存在，跳过创建"
        c.mkdir(f"{BASE}/{A1}", SUB)
        assert c.exists(f"{BASE}/{A1}", SUB)
        return "创建并可查到"

    @step(f"rename 改名 {A1}/{SUB} -> {SUB}2 并立即改回（自我还原，不累积垃圾）")
    def t5():
        if not c.exists(f"{BASE}/{A1}", SUB):
            raise AssertionError(f"缺少测试用子目录 {BASE}/{A1}/{SUB}")
        c.rename(f"{BASE}/{A1}/{SUB}", SUB + "2")
        assert c.exists(f"{BASE}/{A1}", SUB + "2"), "改名后目标未出现"
        assert not c.exists(f"{BASE}/{A1}", SUB), "改名后旧名仍在"
        c.rename(f"{BASE}/{A1}/{SUB}2", SUB)          # 还原
        assert c.exists(f"{BASE}/{A1}", SUB), "还原失败"
        return "旧名消失 → 新名出现 → 已还原"

    @step(f"move 移动 {A1}/{SUB} <-> {BASE}（往返，验证不动文件内容）")
    def t6():
        if not c.exists(f"{BASE}/{A1}", SUB):
            raise AssertionError(f"缺少测试用子目录 {BASE}/{A1}/{SUB}")
        c.move([f"{BASE}/{A1}/{SUB}"], BASE, conflict_policy="skip")
        assert c.exists(BASE, SUB), "移出后目标未出现"
        c.move([f"{BASE}/{SUB}"], f"{BASE}/{A1}", conflict_policy="skip")
        assert c.exists(f"{BASE}/{A1}", SUB), "移回失败"
        return "往返移动成功"

    for f in (t1, t2, t3, t4, t5, t6):
        results.append(f())

    left_alt = c.exists(BASE, A1_ALT)
    c.close()
    print(f"\n自检结果：{sum(results)}/{len(results)} 项通过")
    print(f"测试目录：{BASE}/{A1}/（含空目录 {SUB}/）—— 按「禁止删除」约束保留，"
          f"需要清理请用 cleanup_testdirs.py")
    if left_alt:
        print(f"另有早期版本自检遗留：{BASE}/{A1_ALT}/")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

