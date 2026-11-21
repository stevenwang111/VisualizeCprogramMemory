"""教材知识点映射。

把「内存里发生了什么」翻译成「教材里怎么说」。

这是本项目的灵魂所在：光给出一堆地址和十六进制字节，对初学者没有意义；
必须把每个观察结果挂到对应的教材章节上，让「看到一个现象」能立刻
「想起一个知识点」。

所有文案集中在这里，方便日后按自己的教材版本增补。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .snapshot import Lesson, VarInfo

# ---------------------------------------------------------------------------
# 教材章节
# ---------------------------------------------------------------------------

CH4 = "第4章 函数"
CH5 = "第5章 数组"
CH6 = "第6章 指针"
CH7 = "第7章 字符串"

# ---------------------------------------------------------------------------
# 基础模板
# ---------------------------------------------------------------------------

_STORAGE_LESSONS = {
    "data": (
        CH4,
        "全局 / 静态变量的存储位置",
        "这个变量在 .data 段 —— 也就是全局与静态变量区。程序还没执行到 main，"
        "操作系统就已经把它放进内存了，它在整个程序运行期间一直存在。"
        "这正是它叫「静态存储」的原因：生命期 = 程序生命期。",
    ),
    "bss": (
        CH4,
        "未初始化全局变量的存储位置",
        "这个变量在 .bss 段。它和 .data 的区别只是「有没有初始值」——"
        "未初始化的全局变量会被操作系统自动清零，所以它的值是确定的 0，"
        "而不是随机数。这是全局变量相对局部变量的一个安全优势。",
    ),
    "rdata": (
        CH7,
        "只读数据区",
        "这个变量在 .rdata 段（只读数据区）。字符串字面量和 const 全局变量"
        "都住在这里。程序运行期间这一段是只读的 —— 你可以读，但写了就会崩。",
    ),
    "stack": (
        CH4,
        "自动变量的存储位置",
        "这个变量在栈上。自动变量（函数内部没有写 static 的普通变量）"
        "在函数被调用时才分配空间，函数一返回就被回收。"
        "所以栈是「后进先出」：最深的那次调用，最晚被释放。",
    ),
    "heap": (
        CH4,
        "堆上的变量",
        "这块内存来自堆。堆上的空间由程序员通过 malloc 申请、通过 free 释放，"
        "编译器不替你管。忘记 free 就是内存泄漏。",
    ),
}


def _ref(addr: Optional[int], name: Optional[str] = None) -> Dict[str, Any]:
    ref: Dict[str, Any] = {}
    if addr is not None:
        ref["address"] = addr
    if name:
        ref["name"] = name
    return ref


def _is_pointer_to_pointer(v: VarInfo) -> bool:
    """判断一个指针是否指向另一个指针变量。"""
    if not v.is_pointer or v.pointer_target is None:
        return False
    # 目标地址落在某个指针变量的地址上？
    return False  # 需要全量变量表，交给 analyze 处理


def analyze(
    frames: List[Dict[str, Any]],
    globals_: List[Dict[str, Any]],
    regions: List[Dict[str, Any]],
    depth: int = 0,
    stack_bytes: int = 0,
) -> List[Lesson]:
    """根据当前程序状态生成教学提示。"""
    lessons: List[Lesson] = []
    seen_topics = set()

    def add(chapter, topic, message, refs=None, level="info"):
        if topic in seen_topics:
            return
        seen_topics.add(topic)
        lessons.append(
            Lesson(
                chapter=chapter,
                topic=topic,
                message=message,
                refs=refs or [],
                level=level,
            )
        )

    # 把所有变量拉平成一个列表，方便做交叉分析
    all_vars: List[Dict[str, Any]] = []
    for fr in frames:
        for v in fr.get("vars", []):
            item = dict(v)
            item["_frame"] = fr.get("index")
            item["_func"] = fr.get("func")
            all_vars.append(item)
    for g in globals_:
        item = dict(g)
        item["_frame"] = None
        item["_func"] = "<全局>"
        all_vars.append(item)

    addr_to_var: Dict[int, Dict[str, Any]] = {}
    for v in all_vars:
        a = v.get("address")
        if a is not None and v.get("size"):
            addr_to_var[a] = v

    # ------------------------------------------------------------------
    # 1. 递归检测 —— 同一个函数名出现多次
    # ------------------------------------------------------------------
    func_counts: Dict[str, int] = {}
    for fr in frames:
        func_counts[fr.get("func", "?")] = func_counts.get(fr.get("func", "?"), 0) + 1
    recursive_funcs = [f for f, c in func_counts.items() if c > 1 and f not in ("?", "main")]

    if recursive_funcs:
        fname = recursive_funcs[0]
        count = func_counts[fname]
        refs = [
            _ref(fr.get("rsp"), "%s() 第 %d 层" % (fname, fr.get("index")))
            for fr in frames
            if fr.get("func") == fname
        ]
        add(
            CH4,
            "递归与堆栈",
            "你现在处于递归调用中：%s() 一共在栈上有 %d 层。\n\n"
            "每一次调用都在栈上开辟一块新的空间，各自保存一份自己的变量副本。"
            "所以你在同一个函数里看到的 %s 变量，其实是 %d 个互不相同的变量 —— "
            "它们住在栈上不同的位置，值也不一样。\n\n"
            "栈是后进先出的：最深的那一层（第 %d 层）最先返回，"
            "它的空间也最先被回收。"
            % (fname, count, fname, count, count - 1),
            refs,
        )

    # ------------------------------------------------------------------
    # 2. 调用栈深度
    # ------------------------------------------------------------------
    if depth >= 2:
        add(
            CH4,
            "函数调用与栈帧",
            "当前调用栈深度为 %d 层，占用了 %d 字节的栈空间。\n\n"
            "每调用一个函数，就会在栈上压入一个新的「栈帧」，用来存放"
            "该函数的参数与局部变量。函数返回时，这块空间被弹出（回收）。\n\n"
            "栈从高地址向低地址增长 —— 你可以在内存视图里看到，"
            "越深的一层，地址反而越小。" % (depth, stack_bytes),
            [_ref(fr.get("rsp"), fr.get("func")) for fr in frames[:3]],
        )

    # ------------------------------------------------------------------
    # 3. 存储类型 —— 第4章
    # ------------------------------------------------------------------
    for v in all_vars:
        storage = v.get("storage")
        if storage in _STORAGE_LESSONS and v.get("scope") in ("global", "static"):
            chapter, topic, message = _STORAGE_LESSONS[storage]
            add(
                chapter,
                topic,
                message + "\n\n变量 %s 的类型是 %s，占 %d 字节，地址 0x%016X。"
                % (v.get("name"), v.get("type"), v.get("size") or 0, v.get("address") or 0),
                [_ref(v.get("address"), v.get("name"))],
            )
            break

    # ------------------------------------------------------------------
    # 4. 参数传值 —— 第4章
    # ------------------------------------------------------------------
    args = [v for v in all_vars if v.get("scope") == "arg"]
    if args and frames:
        add(
            CH4,
            "参数按值传递",
            "当前栈帧里有 %d 个参数：C 语言的函数参数一律是「按值传递」的。\n\n"
            "所谓按值传递，就是函数拿到的是调用者变量的一个副本。"
            "在函数里改这个参数，改的是副本，不会影响调用者的那个变量。\n\n"
            "如果想在函数里真正改变调用者的变量，就要传指针（第6章）。"
            % len(args),
            [_ref(a.get("address"), a.get("name")) for a in args[:6]],
        )

    # ------------------------------------------------------------------
    # 5. 数组 —— 第5章
    # ------------------------------------------------------------------
    arrays = [v for v in all_vars if v.get("array_length")]
    if arrays:
        v = arrays[0]
        add(
            CH5,
            "数组在内存中是连续的一块",
            "变量 %s 是数组，类型 %s，共 %d 个元素。\n\n"
            "数组的元素在内存里是紧挨着排列的 —— 在内存视图里能看到它们连成一片。"
            "所以 a[i] 这种写法，编译器实际算的是「首地址 + i × 元素大小」。\n\n"
            "也正因为元素是连续排列的，数组传进函数时会退化成指针，"
            "函数只知道首地址，不知道数组有多长。"
            % (v.get("name"), v.get("type"), v.get("array_length")),
            [_ref(v.get("address"), v.get("name"))],
        )

    # ------------------------------------------------------------------
    # 6. 指针 —— 第6章
    # ------------------------------------------------------------------
    pointers = [v for v in all_vars if v.get("is_pointer")]
    if pointers:
        v = pointers[0]
        target = v.get("pointer_target")
        target_var = addr_to_var.get(target) if target is not None else None

        msg = "变量 %s 是一个指针，类型 %s。\n\n" % (v.get("name"), v.get("type"))
        msg += "指针变量本身占 %d 字节（64 位系统上是 8 字节），" % (v.get("size") or 0)
        msg += "它里面存的不是数据，而是另一个东西的地址：\n\n"
        msg += "  &%s  →  %s 自身的地址（0x%016X）\n" % (
            v.get("name"), v.get("name"), v.get("address") or 0)
        msg += "  %s    →  0x%016X  ← 这里存的值\n" % (v.get("name"), target or 0)
        if target_var is not None:
            msg += "  *%s   →  %s = %s\n" % (
                v.get("name"), target_var.get("name"), target_var.get("value"))
        msg += (
            "\n关键：指针变量的地址，和它指向的东西的地址，是两个不同的数字。"
            "&取的是前者，*解引用得到的是后者。"
        )

        refs = [_ref(v.get("address"), v.get("name"))]
        if target is not None:
            refs.append(_ref(target, target_var.get("name") if target_var else None))
        add(CH6, "指针变量里存的是地址", msg, refs)

    # ------------------------------------------------------------------
    # 7. 多级指针 —— 第6章
    # ------------------------------------------------------------------
    for v in pointers:
        target = v.get("pointer_target")
        if target is None:
            continue
        tv = addr_to_var.get(target)
        if tv is not None and tv.get("is_pointer"):
            add(
                CH6,
                "多级指针（指针的指针）",
                "%s 是一个指向指针的指针。\n\n"
                "沿着它走一圈：\n"
                "  %s        存着 0x%016X\n"
                "  那个地址上的 %s  存着 0x%016X\n"
                "  再解引用一次，才是真正的数据\n\n"
                "这就是为什么 C 的指针类型要写成 `int **` —— "
                "每一个 * 就多一层「先取出地址、再取值」。"
                % (
                    v.get("name"),
                    v.get("name"), target,
                    tv.get("name"), tv.get("pointer_target") or 0,
                ),
                [_ref(v.get("address"), v.get("name")), _ref(target, tv.get("name"))],
            )
            break

    # ------------------------------------------------------------------
    # 8. 指针数组 vs 指向数组的指针 —— 第6章
    # ------------------------------------------------------------------
    for v in all_vars:
        if not v.get("is_pointer"):
            continue
        t = v.get("pointer_target")
        if t is None:
            continue
        # 目标落在某个数组的区间内 → 指向数组的指针
        for arr in arrays:
            a_addr = arr.get("address")
            a_len = arr.get("array_length") or 0
            elem = (arr.get("size") or 0) // max(a_len, 1)
            if a_addr is not None and a_addr <= t < a_addr + (a_len * elem):
                add(
                    CH6,
                    "指向数组的指针",
                    "%s 指向的是数组 %s 的内部（偏移 %d 字节）。\n\n"
                    "注意区分两个概念：\n"
                    "  指向数组的指针   `int (*p)[3]`  —— p 是一个指针，它指向整个数组\n"
                    "  指针数组         `int *p[3]`    —— p 是数组，每个元素都是指针\n\n"
                    "括号的位置决定了一切。"
                    % (v.get("name"), arr.get("name"), t - a_addr),
                    [_ref(v.get("address"), v.get("name")), _ref(t, arr.get("name"))],
                )
                break

    # ------------------------------------------------------------------
    # 9. 字符串 —— 第7章
    # ------------------------------------------------------------------
    strings = [v for v in all_vars if v.get("is_string")]
    if strings:
        v = strings[0]
        add(
            CH7,
            "字符串常量在哪里",
            "%s 指向的是一个字符串常量：\"%s\"\n\n"
            "字符串字面量在编译期就写进了程序的只读数据区（.rdata），"
            "地址是 0x%016X，不是栈也不是堆。\n\n"
            "所以对它执行 `*p = 'X'` 会崩 —— 那段内存是只读的。"
            % (v.get("name"), v.get("preview"), v.get("pointer_target") or 0),
            [_ref(v.get("pointer_target"), "字符串常量"),
             _ref(v.get("address"), v.get("name"))],
        )

    # 字符数组 vs 字符指针
    for v in all_vars:
        if v.get("array_length") and v.get("type") and "char" in v.get("type", ""):
            add(
                CH7,
                "字符数组 vs 字符指针",
                "%s 是一个字符数组（%s），字符直接存在它自己这块内存里。\n\n"
                "而用 `char *s = \"...\"` 定义的字符指针，字符串本体在只读区，"
                "指针里只存着那个地址。\n\n"
                "所以字符数组是可以被修改的，字符指针指向的字面量不行。"
                % (v.get("name"), v.get("type")),
                [_ref(v.get("address"), v.get("name"))],
            )
            break

    # ------------------------------------------------------------------
    # 10. 未初始化的局部变量
    # ------------------------------------------------------------------
    uninit = [v for v in all_vars if v.get("scope") == "local"
              and v.get("decl_line") is not None and not v.get("initialized", True)]
    if uninit:
        v = uninit[0]
        add(
            CH4,
            "未初始化的自动变量",
            "%s 还没有被赋值。\n\n"
            "GDB 停在某一行时，表示「这一行还没执行」。%s 的声明在第 %d 行，"
            "程序还没走到那里，所以这块内存里是**上一次使用时留下的残留数据**。\n\n"
            "这就是为什么 C 语言要求局部变量使用前必须先赋值 —— "
            "编译器不会帮你把栈上的垃圾清零（只有全局变量会被操作系统清零）。"
            % (v.get("name"), v.get("name"), v.get("decl_line")),
            [_ref(v.get("address"), v.get("name"))],
            level="warn",
        )

    # ------------------------------------------------------------------
    # 11. 栈与堆的对比（同时出现时）
    # ------------------------------------------------------------------
    stack_vars = [v for v in all_vars if v.get("storage") == "stack"]
    heap_vars = [v for v in all_vars if v.get("storage") == "heap"]
    if stack_vars and heap_vars:
        add(
            CH4,
            "栈与堆的分工",
            "当前程序里同时存在栈变量和堆上的数据。\n\n"
            "  栈：%s —— 随函数进入而创建、返回而消失，不需要手动管理\n"
            "  堆：%s —— 由 malloc 申请、free 释放，生命周期由程序员掌控\n\n"
            "一句话记法：栈是自动的、快的、有限的；堆是手动���、灵活的、容易泄漏的。"
            % (
                "、".join(v.get("name", "?") for v in stack_vars[:4]),
                "、".join(v.get("name", "?") for v in heap_vars[:4]),
            ),
            [],
        )

    # ------------------------------------------------------------------
    # 12. 结构体的内存布局（第8章，选做）
    # ------------------------------------------------------------------
    structs = [
        v for v in all_vars
        if v.get("type") and ("struct " in v.get("type", "") or "union " in v.get("type", ""))
    ]
    if structs:
        v = structs[0]
        is_union = "union " in v.get("type", "")
        if is_union:
            add(
                "第8章 联合体",
                "联合体：所有成员共用一块内存",
                "%s 是一个联合体，大小 %d 字节。\n\n"
                "联合体的所有成员**共用同一块内存**，大小等于最大的那个成员。"
                "所以给联合体赋一个值，再按另一种类型读出来，得到的是同一批字节的"
                "另一种解释 —— 这也正是判断字节序（大小端）的常用办法。\n\n"
                "在内存视图里切换变量卡片，就能看到同一地址被解释成不同类型时的差别。"
                % (v.get("name"), v.get("size") or 0),
                [_ref(v.get("address"), v.get("name"))],
            )
        else:
            add(
                "第8章 结构体",
                "结构体的成员排列与对齐填充",
                "%s 是一个结构体，类型 %s，占 %d 字节。\n\n"
                "结构体的成员在内存里是**按声明顺序紧挨着**排的，"
                "但编译器会为了对齐（让 CPU 能一次读完整）插入若干填充字节。\n\n"
                "所以 sizeof(struct) 往往大于各成员大小之和 —— 多出来的就是填充。"
                "在内存视图里用鼠标悬停看每个字节，就能找到那些没有对应成员的「空隙」。"
                % (v.get("name"), v.get("type"), v.get("size") or 0),
                [_ref(v.get("address"), v.get("name"))],
            )

    return lessons
