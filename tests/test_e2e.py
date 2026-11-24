"""端到端冒烟测试：编译 → 起 GDB → 采集 → 单步 → 校验。

运行：
    python tests/test_e2e.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdbg.session import DebugSession  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def check(label, condition, detail=""):
    status = "OK  " if condition else "FAIL"
    print("  [%s] %s%s" % (status, label, ("  — " + str(detail)) if detail and not condition else ""))
    return bool(condition)


def wait_for(session, predicate, timeout=4.0, step=0.05):
    """等到快照满足条件，返回最后一次的快照。

    比固定 sleep(0.5) 稳：重启后程序先在 main 停一下（监视线程会先发一份
    入口快照），再跑到断点，机器一忙那 0.5 秒就不够 —— 断言本身没变，
    只是把「等多久」换成「等到满足为止，最多等 timeout」。
    """
    deadline = time.time() + timeout
    snap = session.snapshot
    while time.time() < deadline:
        snap = session.snapshot
        if snap is not None and predicate(snap):
            return snap
        time.sleep(step)
    return snap


def test_pointer_example():
    print("\n=== ch06_pointer.c ===")
    session = DebugSession(os.path.join(EXAMPLES, "ch06_pointer.c"))
    ok = True
    try:
        session.start()
        snap = session.snapshot
        if snap is None:
            return check("拿到初始快照", False, "snapshot 为 None")

        # ---- 刚加载：应该是「未执行」状态 ----
        # 加载源码不等于执行源码。跟 Dev-C++ 一样，只有按下「调试」才开始跑。
        ok &= check("启动后是未执行状态", snap.state == "not-started",
                    "state=%s" % snap.state)
        ok &= check("未执行时没有栈帧", len(snap.frames) == 0,
                    "frames=%d" % len(snap.frames))
        ok &= check("未执行时也能看到全局变量",
                    any(v.name == "global_int" for v in snap.globals),
                    [v.name for v in snap.globals])
        ok &= check("未执行时也能看到段布局", len(snap.regions) >= 3,
                    [(r.kind, hex(r.start)) for r in snap.regions])

        # ---- 按「调试」开始执行 ----
        session.command("debug")
        time.sleep(0.3)
        snap = session.snapshot

        ok &= check("调试后停在 main", snap.current_func == "main",
                    "当前函数 = %s" % snap.current_func)
        ok &= check("有全局变量 global_int",
                    any(v.name == "global_int" for v in snap.globals),
                    [v.name for v in snap.globals])
        ok &= check("有栈帧变量 value/p/pp",
                    any(v.name == "p" for fr in snap.frames for v in fr.vars),
                    [v.name for fr in snap.frames for v in fr.vars])

        ok &= check("有内存区域分区", len(snap.regions) >= 3,
                    [(r.kind, hex(r.start)) for r in snap.regions])
        ok &= check("有 HEX 字节数据", len(snap.chunks) >= 1,
                    "chunks=%d" % len(snap.chunks))
        ok &= check("有教学提示", len(snap.lessons) >= 1,
                    [l.topic for l in snap.lessons])

        # 栈区域应该存在
        kinds = {r.kind for r in snap.regions}
        ok &= check("识别出栈区域", "stack" in kinds, kinds)

        # ---- 断点 ----
        # 有断点时，调试应该停在断点那一行
        session.command("break", line=31, on=True)
        time.sleep(0.2)
        ok &= check("断点已记录", 31 in session.breakpoint_lines(),
                    session.breakpoint_lines())
        session.command("restart")
        snap = wait_for(session, lambda s: s.current_line == 31)
        ok &= check("有断点时停在断点行", snap.current_line == 31,
                    "停在第 %d 行" % snap.current_line)

        # 取消断点必须真的落到 GDB 上 —— 以前前端只改了本地标记，
        # 后端还留着一份，下一次状态同步又把红点刷回来了。
        session.command("break", line=31, on=False)
        time.sleep(0.2)
        ok &= check("断点可以取消", 31 not in session.breakpoint_lines(),
                    session.breakpoint_lines())
        ok &= check("取消后 GDB 里也不剩断点",
                    session.gdb.send("-break-list", raise_on_timeout=False)
                    .get("raw", "").count("bkpt={") == 0,
                    "断点表还有残留")

        # 同一行重复设置不该在 GDB 里留下两个断点（删一个还剩一个那种坑）
        session.command("break", line=28, on=True)
        session.command("break", line=28, on=True)
        time.sleep(0.2)
        session.command("break", line=28, on=False)
        time.sleep(0.2)
        leftover = session.gdb.send("-break-list", raise_on_timeout=False).get("raw", "")
        ok &= check("重复设置同一行只留一个断点",
                    28 not in session.breakpoint_lines()
                    and leftover.count("bkpt={") == 0,
                    "断点表还有残留：%s" % leftover[-200:])

        # ---- 单步到指针初始化之后 ----
        # 停在 main 入口时，p / pp 还没有被赋值（此时它们的内容是未初始化
        # 的栈垃圾值，实测是一个 0x7ff7... 的残留地址），必须先执行过
        # `int *p = &value;` 才能验证指向关系。
        # 注意：GDB 停在某行时表示「该行还没执行」，所以要走到下一行。
        for _ in range(20):
            session.command("next")
            time.sleep(0.05)
            snap = session.snapshot
            if snap and snap.current_line >= 26:
                break

        p = None
        value = None
        pp = None
        for fr in snap.frames:
            for v in fr.vars:
                if v.name == "p":
                    p = v
                elif v.name == "value":
                    value = v
                elif v.name == "pp":
                    pp = v

        ok &= check("p 指向 value", p and value and p.pointer_target == value.address,
                    "p.target=%s value.addr=%s" % (
                        p.pointer_target if p else None,
                        value.address if value else None))
        ok &= check("多级指针 pp 指向 p", pp and p and pp.pointer_target == p.address,
                    "pp.target=%s p.addr=%s" % (
                        pp.pointer_target if pp else None,
                        p.address if p else None))

        # ---- 继续单步 ----
        before = snap.current_line
        session.command("next")
        time.sleep(0.2)
        snap2 = session.snapshot
        ok &= check("单步后行号前进", snap2.current_line != before,
                    "%s -> %s" % (before, snap2.current_line))

        # ---- 清空界面 / 强制结束 ----
        session.command("break", line=24, on=True)
        time.sleep(0.2)
        r = session.command("clear")
        ok &= check("清空界面返回 cleared", bool(r.get("ok")) and bool(r.get("cleared")), r)
        ok &= check("清空后没有快照", session.snapshot is None, session.snapshot)
        ok &= check("清空后阶段回到未执行", session.phase == "not-started", session.phase)
        ok &= check("清空不动断点", 24 in session.breakpoint_lines(),
                    session.breakpoint_lines())
        session.command("debug")
        time.sleep(0.4)
        ok &= check("清空之后还能调试", session.snapshot is not None
                    and session.snapshot.current_line == 24,
                    session.snapshot.current_line if session.snapshot else None)

        pid = getattr(session, "_target_pid", None)
        ok &= check("记住了程序 PID（长按停止要用）", bool(pid), pid)
        r = session.command("force-kill")
        ok &= check("强制结束程序", bool(r.get("ok")), r)
        session.command("break", line=24, on=False)
        time.sleep(0.2)

        # ---- 递归 ----
        print("\n=== ch04_recursion.c ===")
        sess2 = DebugSession(os.path.join(EXAMPLES, "ch04_recursion.c"))
        try:
            sess2.start()
            sess2.command("debug")
            time.sleep(0.3)
            # 连续步进直到进入 fact
            entered = False
            for _ in range(40):
                sess2.command("step")
                time.sleep(0.05)
                s = sess2.snapshot
                if s and s.current_func == "fact":
                    entered = True
                    break
            ok &= check("能单步进入 fact()", entered,
                        sess2.snapshot.current_func if sess2.snapshot else "?")

            if entered:
                # 继续深入，看递归深度
                depth_seen = 0
                for _ in range(20):
                    sess2.command("step")
                    time.sleep(0.05)
                    s = sess2.snapshot
                    if s:
                        depth_seen = max(depth_seen, s.depth)
                    if s and s.depth >= 3:
                        break
                s = sess2.snapshot
                ok &= check("递归产生多层栈帧", depth_seen >= 3, "最大深度=%d" % depth_seen)

                # 同名变量在不同帧
                if s:
                    n_addrs = []
                    for fr in s.frames:
                        for v in fr.vars:
                            if v.name == "n":
                                n_addrs.append(v.address)
                    ok &= check("同名变量 n 在不同帧有不同地址",
                                len(n_addrs) >= 2 and len(set(n_addrs)) == len(n_addrs),
                                n_addrs)
        finally:
            sess2.shutdown()

    finally:
        session.shutdown()

    return ok


def main():
    print("cdbg 端到端冒烟测试")
    ok = test_pointer_example()
    print("\n" + ("全部通过 ✔" if ok else "存在失败 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
