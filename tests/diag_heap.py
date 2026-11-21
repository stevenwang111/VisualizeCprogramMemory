"""堆跟踪的临时诊断脚本。"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdbg.session import DebugSession  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def main():
    s = DebugSession(os.path.join(EXAMPLES, "ch04_heap.c"))
    s.start()
    print("notes:", s.instrument_notes)

    for i in range(12):
        snap = s.snapshot
        print("step %2d: file=%s line=%d func=%s" % (
            i, os.path.basename(snap.current_file or "?"),
            snap.current_line, snap.current_func))
        s.command("next")
        time.sleep(0.05)

    snap = s.snapshot
    print("--- heap blocks ---")
    for h in snap.heap:
        print("   %016X size=%-5d %s" % (h.address, h.size, h.label))
    print("total=%d freed=%d" % (snap.heap_total, len(snap.heap_freed)))
    s.shutdown()


if __name__ == "__main__":
    main()
