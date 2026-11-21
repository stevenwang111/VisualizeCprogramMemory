"""`/proc/<pid>/maps` 解析的单元测试（不需要 Linux 运行环境）。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdbg import memmap_linux  # noqa: E402

#: 固定文本：包含栈、堆、文件映射、匿名块、[vdso]、以及一行坏数据
MAPS = """\
55d4c1a2b000-55d4c1a2c000 r--p 00000000 08:02 1234567  /usr/bin/prog
55d4c1a2c000-55d4c1a2d000 r-xp 00001000 08:02 1234567  /usr/bin/prog
55d4c1a2d000-55d4c1a2e000 r--p 00002000 08:02 1234567  /usr/bin/prog
55d4c1a2e000-55d4c1a2f000 r--p 00002000 08:02 1234567  /usr/bin/prog
55d4c1a2f000-55d4c1a30000 rw-p 00003000 08:02 1234567  /usr/bin/prog
55d4c1a30000-55d4c1a31000 rw-p 00000000 00:00 0
55d4c1a31000-55d4c1a32000 rw-p 00000000 00:00 0
55d4c1a32000-55d4c1a33000 rw-p 00000000 00:00 0  [heap]
7f0a00000000-7f0a00021000 rw-p 00000000 00:00 0
7f0a00021000-7f0a00022000 rw-p 00000000 00:00 0
7f0a00022000-7f0a00023000 r--p 00000000 00:00 0  [vvar]
7f0a00023000-7f0a00025000 r-xp 00000000 00:00 0  [vdso]
7ffd3c400000-7ffd3c421000 rw-p 00000000 00:00 0  [stack]
this is not a maps line
7ffd3c421000-7ffd3c421000 rw-p 00000000 00:00 0  [bad]
"""


def check(label, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", label,
                           ("  — " + str(detail)) if detail and not cond else ""))
    return bool(cond)


def main():
    print("memmap_linux 解析测试")
    regions = memmap_linux.parse_maps_text(MAPS)
    ok = True

    ok &= check("坏行被忽略、正常行全部解析", len(regions) == 10, len(regions))

    image = [r for r in regions if r.is_image]
    ok &= check("文件映射识别为 image", len(image) == 5, len(image))
    ok &= check("image 带文件路径", image and image[0].detail == "/usr/bin/prog",
                image[0].detail if image else None)

    stack = [r for r in regions if r.detail == "[stack]"]
    ok &= check("识别 [stack]", len(stack) == 1, stack)

    heap = [r for r in regions if r.detail == "[heap]"]
    ok &= check("识别 [heap]", len(heap) == 1, heap)

    special = [r for r in regions if r.detail in ("[vdso]", "[vvar]")]
    ok &= check("特殊映射归为 other", len(special) == 2, special)

    # 三个相邻可写匿名块应合并：两个纯匿名 + [heap]
    anon_start = 0x55D4C1A30000
    anon_end = 0x55D4C1A33000
    merged = [r for r in regions if r.base == anon_start and r.end == anon_end]
    ok &= check("相邻可写匿名块被合并", len(merged) == 1, merged)

    # 栈边界
    bounds = memmap_linux.stack_bounds(regions, 0x7FFD3C401000)
    ok &= check("stack_bounds 命中 [stack]",
                bounds == (0x7FFD3C400000, 0x7FFD3C421000), bounds)

    # 堆指针归属：指针在合并块里
    hb = memmap_linux.heap_bounds(regions, [0x55D4C1A31500])
    ok &= check("堆指针归属合并后的匿名块",
                hb == [(anon_start, anon_end)], hb)

    # 文件映射里的指针不能算堆
    hb2 = memmap_linux.heap_bounds(regions, [0x55D4C1A2C050])
    ok &= check("文件映射里的指针不算堆", hb2 == [], hb2)

    # 栈里的指针不能算堆
    hb3 = memmap_linux.heap_bounds(regions, [0x7FFD3C401000])
    ok &= check("栈里的指针不算堆", hb3 == [], hb3)

    # 权限映射
    ok &= check("权限映射：r-xp → 可执行可读", image[1].protect_text() == "可执行可读",
                image[1].protect_text())

    # 异常输入不抛异常
    ok &= check("空文本返回空列表", memmap_linux.parse_maps_text("") == [])
    ok &= check("乱码不抛异常", memmap_linux.parse_maps_text("xxxx\nyyyy") == [])

    # 不用 ✔ / ✘：Windows 的 GBK 控制台编码不了会直接抛 UnicodeEncodeError
    print("\n" + ("全部通过" if ok else "存在失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
