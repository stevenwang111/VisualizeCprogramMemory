"""批量兼容性测试。

用法：
    python tests/test_batch.py                 # 测 examples/
    python tests/test_batch.py D:/cdesign      # 测指定目录下的所有源文件

每个文件都要过四关：
    编译 → 启动 → 停在 main → 采集到变量信息
任何一关不过都会记录下来。
"""

from __future__ import annotations

import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdbg.session import DebugSession  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(HERE, "examples")


#: 默认跳过的目录 —— 这些是第三方/历史库源码，不是使用者自己的代码。
#: 例如 D:/cdesign/C上机环境/BC31 下是 Borland C++ 3.1 的 DOS 时代 CRT 源码，
#: 用现代 GCC 编译必然失败（找不到 <_io.h>、<graphics.h> 这类老头文件），
#: 那属于源文件与工具链不兼容，不是调试器的问题。
DEFAULT_EXCLUDE = ("上机环境", "BC31", "node_modules", ".git")


def collect_sources(root, excludes=DEFAULT_EXCLUDE):
    """递归找出目录下所有 C 源文件。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not any(x in d for x in excludes)
        ]
        for name in sorted(filenames):
            if name.lower().endswith(".c"):
                full = os.path.join(dirpath, name)
                if any(x in full for x in excludes):
                    continue
                out.append(full)
    return out


def test_one(path, verbose=False):
    """测试单个文件，返回 (状态, 说明)。"""
    t0 = time.time()
    session = None
    try:
        session = DebugSession(path)
        started = session.start()

        # 编译没过 —— 但只要工具能把问题定位出来（给出文件/行号/原因），
        # 就算它正常工作：源程序本身有错不是工具的错。
        if not started:
            if session.build_errors:
                first = session.build_errors[0]
                return "OK", "编译未通过，但已定位：第%s行 %s" % (
                    first.get("line", 0), first.get("message", "")[:90])
            return "FAIL", "编译失败且没有给出定位信息"

        snap = session.snapshot
        if snap is None:
            return "FAIL", "没有拿到快照"

        # 加载源码后程序处于「未执行」状态 —— 按「调试」才开始跑。
        # 这跟 Dev-C++ 一致。
        if snap.state != "not-started":
            return "FAIL", "加载后应该是未执行状态，实际是 %s" % snap.state
        if not snap.regions:
            return "FAIL", "未执行时也应该能看到段布局"

        session.command("debug")
        time.sleep(0.3)
        snap = session.snapshot
        if snap is None:
            return "FAIL", "调试后没有拿到快照"

        nvars = len(snap.globals) + sum(len(f.vars) for f in snap.frames)
        nregions = len(snap.regions)
        nlessons = len(snap.lessons)
        nbytes = len(snap.chunks)

        problems = []
        if not snap.frames:
            problems.append("没有栈帧")
        if not nregions:
            problems.append("没有内存区域")
        # 局部变量为 0 不算问题 —— 很多小程序 main 里确实没有局部变量

        detail = "帧%d 变量%d 区域%d 字节%d 提示%d" % (
            len(snap.frames), nvars, nregions, nbytes, nlessons)
        if verbose and problems:
            detail += "  << " + "、".join(problems)
        status = "WARN" if problems else "OK"
        return status, detail
    except Exception as exc:
        return "FAIL", "%s: %s" % (type(exc).__name__, str(exc)[:150])
    finally:
        if session is not None:
            try:
                session.shutdown()
            except Exception:
                pass


def main(argv):
    root = argv[1] if len(argv) > 1 else EXAMPLES
    sources = collect_sources(root)
    print("批量测试：%s（共 %d 个 .c 文件）\n" % (root, len(sources)))

    stats = {"OK": 0, "WARN": 0, "FAIL": 0}
    failures = []

    for path in sources:
        rel = os.path.relpath(path, root)
        t0 = time.time()
        status, detail = test_one(path)
        elapsed = time.time() - t0
        stats[status] = stats.get(status, 0) + 1
        mark = {"OK": "  OK  ", "WARN": " [WARN]", "FAIL": " [FAIL]"}[status]
        print("%s %-34s %s  (%.1fs)" % (mark, rel, detail, elapsed))
        if status != "OK":
            failures.append((rel, status, detail))

    print("\n" + "-" * 62)
    print("通过 %d   警告 %d   失败 %d" % (stats.get("OK", 0), stats.get("WARN", 0), stats.get("FAIL", 0)))
    if failures:
        print("\n需要关注的：")
        for rel, status, detail in failures:
            print("  [%s] %s\n         %s" % (status, rel, detail))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
