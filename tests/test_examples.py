"""所有示例程序的冒烟验证：能否编译、启动、采集到变量。"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdbg.session import DebugSession  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def main():
    files = sorted(
        f for f in os.listdir(EXAMPLES) if f.endswith(".c")
    )
    ok_all = True
    for name in files:
        path = os.path.join(EXAMPLES, name)
        try:
            s = DebugSession(path)
            s.start()
            snap = s.snapshot
            nvars = len(snap.globals) + sum(len(f.vars) for f in snap.frames)
            print("  [OK  ] %-22s 停在 %-14s 变量 %2d  区域 %d  提示 %d" % (
                name, "%s:%d" % (snap.current_func, snap.current_line),
                nvars, len(snap.regions), len(snap.lessons)))
            # 这里采到的是「未执行」快照：按 Dev-C++ 的语义，程序还没跑，
            # 所以没有栈帧、没有局部变量 —— 只有全局变量和段布局可得。
            # 因此判断标准不能是「有没有变量」（全是局部变量的示例必然为 0），
            # 而是「符号和段布局有没有加载成功」。
            if not snap.source_file or len(snap.regions) < 3:
                print("         ^ 警告：符号表或段布局没加载出来")
                ok_all = False
            s.shutdown()
        except Exception as exc:
            print("  [FAIL] %-22s %s: %s" % (name, type(exc).__name__, str(exc)[:120]))
            ok_all = False
    print("\n" + ("全部示例可用 ✔" if ok_all else "存在失败 ✘"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
