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

### 2. 编译器不合适时自动补一套

**背景**：装了 Visual Studio 的机器会扫到 `cl.exe`（MSVC）。`compiler_scanner.py` 目前把它当合法候选（`_detect_family` 认 `cl.exe`，约第 408、424 行），但 **MSVC 产出 PDB 调试信息，GDB 只认 DWARF**——用它编译出来的 exe 能编译、能启动，却看不到变量、行号和调用栈，等于没调试。这不是"少支持几个特性"，是调试信息格式根本对不上。

**要做**：

- without runtime 版增加"编译器不可用"判定：扫到的候选里只有 MSVC、或 gdb 不带 Python 支持时，不要直接拿它编译，而是进入补装流程。
- 补装流程：拉一份 MinGW-w64（gcc + gdb）到本地固定目录，建议 `%LOCALAPPDATA%\cdbg\toolchain\`（别写进程序目录，避免 UAC 和每次解压被覆盖），校验哈希，再调用现有的 `write_config` 把路径写进 `config/toolchain.conf`，下次启动直接复用。
- 国内下载慢：支持镜像源、断点续传、失败重试；下载控件不要引第三方库（本项目坚持纯标准库）。
- `_detect_family` 对 MSVC 的处理要重新想：要么从候选里摘掉，要么只在"没有别的可用编译器"时留作占位并触发补装，别让它被静默选中。

> 个人开发者拿不到 OV/EV 代码签名证书（要企业主体核验），这一项不列入 TODO。偶尔被 Defender 误报走微软免费误报申诉即可。

---

## 二、交互与界面

### 3. 程序退出后保留控制台 + 收尾提示

**现状**：被调试程序有独立控制台窗口（和 Dev-C++ 调试时一致），程序一退出窗口就销毁，学生看不到最后几行 `printf`。代码里对应 `gdb_session.py` 约第 231 行的 TODO 注释，README「程序的输入输出」一节也有记录。

**要做**：另挂一个用 `AttachConsole` 附着到被调试进程的保活进程守着那个控制台，程序退出后由它打印 Dev-C++ 风格的两行并等一次按键：

```text
--------------------------------
Process exited after 8.978 seconds with return value 0
请按任意键继续. . .
```

现在刻意保持和 Dev-C++「调试」时一致（窗口随程序一起消失），**等主要功能稳定后再加**。

---

## 三、平台与适配（暂不排期）

### 4. macOS / ARM64 平台

Windows 与 Linux x86-64 已支持（Linux 用 `/proc/<pid>/maps` 采集内存布局）。
macOS 需要另写 `memmap_darwin.py`（`mach_vm_region` 或 `vmmap`），并适配
Mach-O 段名；ARM64 还需要重新核对寄存器与栈帧模型。两者都暂不排期。

### 5. 更完整的多文件工程

现在只支持"一个主文件 + 若干辅助文件"这种简单结构——工具会自动把定义了缺失符号的 `.c` 一起编译。更复杂的目录结构、多目录 include 还没做。

### 6. 代码段反汇编

`.text`（代码段）目前只展示前 192 字节作示意，不做反汇编。要做的话得引入反汇编能力，和"纯标准库"的约束冲突，需要单独评估。

---

## 记录原则

- 新想法继续往这份里加，写清**现状、动机、实现要点**，别只写一句标题。
- 一旦实现，从这份里删掉，把关键决策补进 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的架构或"已知的坑"章节。
- 许可证相关的决定（尤其是 MIT 保持不变这条）不要随手改，改动前先查证 GNU GPL FAQ 与上游许可证原文。
