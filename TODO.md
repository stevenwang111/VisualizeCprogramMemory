# TODO · 想做但还没做的变更

这份只记**还没实现**的想法，不是当前功能。按优先级和主题分组，供下一位维护者接手时挑着做。

开发相关的现状、架构、约定都在 [`CONTRIBUTING.md`](CONTRIBUTING.md)，使用者文档在 [`README.md`](README.md)。

---

## 一、发布与工具链（优先级最高）

### 1. 双运行时发布包

**现状**：云编译只产出不带编译器的免安装包 `cdbg-windows-x64.zip`（`tools/build_standalone.py`），运行时靠 `compiler_scanner.py` 扫本机的 gcc/gdb。

**要做**：

- 再出一个 **with runtime** 包 `cdbg-windows-x64-with-runtime.zip`，把一套 MinGW-w64 的 gcc + gdb 一起打进去，解压就能调试，不用先装编译器。
- 原本那个改叫 **without runtime**，体积小，给已经配好环境的人用。
- 两套包共用同一套构建脚本，用参数切换是否 include 工具链目录；CI 里两个 artifact 都上传。
- **gdb 必须带 Python 支持**（`gdb --batch -ex "python print(1)"` 能打印 `1` 才算合格），否则 `gdb_probe.py` 加载不了，界面一个变量都显示不出来。

**许可证注意**：内置 gcc/gdb 是把两个 GPL v3 程序与本项目打包分发，属于 GPL 的 "mere aggregation"（单纯聚合），**不要求本项目改成 GPL**，MIT 保持不变。但 with runtime 包里必须一并附带：

- gcc/gdb 各自的 GPL 许可证文本
- 原版权声明，且不改动二进制
- 对应源码的获取途径（官方源码 URL + 具体版本号即可）

学生用 gcc 编译自己的 `.c`，即使链接了 `libgcc`/`libstdc++`，也受 **GCC Runtime Library Exception** 保护，不用开源自己的程序。

---

## 二、交互与界面

### 1. 程序退出后保留控制台 + 收尾提示

**现状**：被调试程序有独立控制台窗口（和 Dev-C++ 调试时一致），程序一退出窗口就销毁，学生看不到最后几行 `printf`。代码里对应 `gdb_session.py` 约第 231 行的 TODO 注释，README「程序的输入输出」一节也有记录。

**要做**：另挂一个用 `AttachConsole` 附着到被调试进程的保活进程守着那个控制台，程序退出后由它打印 Dev-C++ 风格的两行并等一次按键：

```text
--------------------------------
Process exited after 8.978 seconds with return value 0
请按任意键继续. . .
```

现在刻意保持和 Dev-C++「调试」时一致（窗口随程序一起消失），**等主要功能稳定后再加**。

### 2. UI改进
**现状**：UI较为单调且对不同尺寸屏幕适应能力较弱，应用icon不完整

**要做**：完善UI，加入可自定义背景功能，背景支持可变高斯模糊+控件alpha通道

### 3. 用户体验

**要做**：加入新手引导，快速上手

---

## 三、平台与适配（暂不排期）

### 1. macOS / ARM64 平台

Windows 与 Linux x86-64 已支持（Linux 用 `/proc/<pid>/maps` 采集内存布局）。
macOS 需要另写 `memmap_darwin.py`（`mach_vm_region` 或 `vmmap`），并适配
Mach-O 段名；ARM64 还需要重新核对寄存器与栈帧模型。两者都暂不排期。

### 2. 更完整的多文件工程

现在只支持"一个主文件 + 若干辅助文件"这种简单结构——工具会自动把定义了缺失符号的 `.c` 一起编译。更复杂的目录结构、多目录 include 还没做。

### 3. 代码段反汇编

`.text`（代码段）目前只展示前 192 字节作示意，不做反汇编。要做的话得引入反汇编能力，和"纯标准库"的约束冲突，需要单独评估。

---

## 记录原则

- 新想法继续往这份里加，写清**现状、动机、实现要点**，别只写一句标题。
- 一旦实现，从这份里删掉，把关键决策补进 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的架构或"已知的坑"章节。
- 许可证相关的决定（尤其是 MIT 保持不变这条）不要随手改，改动前先查证 GNU GPL FAQ 与上游许可证原文。
