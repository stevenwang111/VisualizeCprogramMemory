"""cdbg — 面向 C 语言初学者的可视化调试器。

设计目标：把「程序内存视图」和「源代码」双向关联起来，让初学者直观看到
变量在哪、指针指向哪、递归时每层栈帧长什么样。
"""

__version__ = "0.1.0"
__all__ = [
    "snapshot", "launcher", "gdb_session", "collector", "memmap", "curriculum",
    "toolchain_config", "compiler_scanner",
]
