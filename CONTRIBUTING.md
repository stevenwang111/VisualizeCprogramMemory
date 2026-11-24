# CONTRIBUTING · 接手与贡献指南

这份给**改代码的人**看：怎么把项目跑起来、架构长什么样、改一处会牵动哪里、改完怎么验证、云端怎么构建。

使用者看 [`README.md`](README.md)（怎么装、怎么操作、界面是什么）。还没做的想法记在 [`TODO.md`](TODO.md)，动手前先扫一眼，别重复造轮子。

> 本地工作区里可能有一个 `cdbg - 副本` 目录，那是备份，不要改它。所有开发都在本目录进行。

---

## 1. 环境（Windows / Linux）

| 依赖 | 说明 |
|---|---|
| Python 3.9+ | 只用标准库，别引入 Flask / FastAPI 之类的 pip 包 |
| GCC | 编译要 `-g -O0`；Windows 实测 `C:\msys64\ucrt64\bin\gcc.exe`，Linux 用 `/usr/bin/gcc` |
| GDB | **必须带 Python 支持**：`gdb --batch -ex "python print(1)"` 能打印 `1` 才算合格 |
| Edge | 前端截图/UI 回归用 `playwright-core`（在 `tools/node_modules`），走 `channel: "msedge"`（Windows） |

工具链配置 `config/toolchain.conf` 是唯一权威来源（扫描生成、读取分离）。首次开箱若配置缺失或路径失效，会自动重扫覆盖，所以整个目录拷到另一台机器也能跑。`CDBG_CC` / `CDBG_GDB` 可临时覆盖，优先级高于配置。

```bash
python cdbg.py --toolchain                    # 看当前用哪个 gcc/gdb、配置在哪
python cdbg.py --scan-compiler                # 重扫 + TUI 选择
python cdbg.py --scan-compiler --select 2     # 非交互直选第 2 套

# Linux / WSL2
sudo apt install gcc gdb
python3 cdbg.py --toolchain
```

**平台差异**：内存布局采集在 Windows 用 `VirtualQueryEx`（`cdbg/memmap_win.py`），
在 Linux 用 `/proc/<pid>/maps`（`cdbg/memmap_linux.py`），统一入口是
`cdbg/memmap.py`。`collector.py` 不感知平台。Linux 开发建议把仓库放在 WSL 的
ext4 文件系统（如 `~/cdbg`），不要在 `/mnt/c` 上做主要编译与测试。

---

## 2. 本地跑起来

```bash
python cdbg.py examples/ch06_pointer.c              # 编译 + GDB + Web + 开浏览器
python cdbg.py prog.c --no-browser --port 8779      # 自己访问 http://127.0.0.1:8779/
python cdbg.py prog.c --headless                    # 终端输出快照，给 IDE/脚本调用
python cdbg.py prog.exe --keep-workdir              # 调试现成 exe，保留临时工作目录
python3 cdbg.py prog --keep-workdir                 # Linux：调试现成可执行文件
python cdbg.py prog.c --no-instrument               # 关掉堆跟踪插桩
```

`cdbg.bat` / `start.bat` 是拖拽和"打开方式"入口，`scan_compiler.bat` 是工具链选择器，内部都是 `python cdbg.py ...`。

**注意**：这些 bat 不 `cd` 到脚本目录，**启动时的 cwd 不一定是项目根目录**。凡是涉及路径的代码都从 `__file__` 定位，别改成相对 cwd。

---

## 3. 架构：两条通道

整套东西的核心设计是**执行控制与数据采集分离**。

```
cdbg.py  main()
   │  toolchain_config.resolve()          —— 这次用哪个 gcc / gdb
   │     ├── 读 config/toolchain.conf 的「设置项2」
   │     └── 配置缺失 / 路径失效 → compiler_scanner 扫全盘，重写配置
   │  编译（launcher + instrument）
   ▼
DebugSession.start()                       session.py  —— 总编排
   │
   ├── GdbSession  ──MI2──> gdb.exe        执行控制：run / next / step / break / continue
   │        │                               （gdb_session.py）
   │        └── 停稳后等快照文件更新
   │
   └── gdb_probe.py（注入 GDB 的 Python 脚本）
            └── 每次 stop 把 JSON 原子写入临时文件
                     │
                     ▼
              Collector.collect()          collector.py —— 聚合成 Snapshot
                     │   + memmap（外部堆栈边界）
                     │   + curriculum（教学提示）
                     ▼
              Snapshot  ──JSON──>  Web ──SSE / REST──>  前端
```

**为什么这么绕**：MI 通道和 GDB console 解释器混用会让输出交错、解析错乱。所以采集走独立脚本 + 约定文件，两边只靠"快照文件更新"这个信号同步。改这一块之前先想清楚别把两条通道并回去。

**为什么是「附加调试器」而不是 attach**：GDB 在 Windows 上 attach 到已有进程会被系统拒绝（`error 5`，Access Denied）。所以 cdbg 采用 launch 模式——由它启动被调试程序，全程伴随运行，和 IDE 里"用调试器启动程序"是同一件事。

另外，GDB 用 GBK 解码路径，中文目录会直接崩。所以源码先复制到**纯 ASCII 临时工作目录**再编译（`launcher.make_workspace`），界面显示的仍是原始路径（`_map_back` 映射回来）。

---

## 4. 文件树与模块地图

```text
cdbg/
├─ cdbg.py                    主入口：编译 → 起 GDB → 起 Web → 开浏览器
├─ cdbg/
│  ├─ launcher.py             编译流程、ASCII 工作目录管理
│  ├─ instrument.py           源码插桩（堆跟踪）
│  ├─ gdb_session.py          MI2 会话管理、命令收发
│  ├─ gdb_probe.py            嵌入 GDB 的采集脚本（变量/栈帧/内存/段）
│  ├─ collector.py            采集结果 → Snapshot 的聚合层
│  ├─ memmap.py               内存布局平台门面（按 sys.platform 分派）
│  ├─ memmap_win.py           Windows：VirtualQueryEx
│  ├─ memmap_linux.py         Linux：/proc/<pid>/maps
│  ├─ heap_ledger.py          堆账本解析
│  ├─ curriculum.py           教材知识点映射（提示文案的唯一来源）
│  ├─ snapshot.py             数据模型
│  ├─ session.py              调试会话编排
│  ├─ web_server.py           HTTP + REST + SSE
│  └─ browser.py              打开浏览器
├─ web/                       前端（纯静态，无构建步骤）
├─ shim/                      堆跟踪插桩的 shim 头与实现
├─ examples/                  各章示例程序
└─ tests/                     端到端测试
```

### 后端 `cdbg/`

| 文件 | 职责 | 改动时注意 |
|---|---|---|
| `session.py` | 会话编排：`start` / `command` / `shutdown`、快照发布、phase 管理 | **命令分发总入口**。新增调试动作先在 `command()` 加分支，再配 GdbSession 和前端按钮 |
| `gdb_session.py` | MI2 协议收发、`*stopped` 事件、等待快照文件 | 不要混用 console 命令；`_GDB_SETTINGS` 的项不能带 `set` 前缀 |
| `gdb_probe.py` | 跑在 **GDB 进程内**的 Python：变量、栈帧、字节、段边界 | 这里是 GDB 的 Python 2/3 环境，不是主进程的 Python；异常会静默，注意兜底 |
| `launcher.py` | 工具查找、ASCII 工作目录、编译、编译/链接错误诊断 | `DEBUG_FLAGS` 里每个 flag 都有教学含义，别随手删。**gcc/gdb 的解析入口在这里**（`find_compiler` / `find_gdb`），顺序是 环境变量 → 配置 → PATH → 常见目录 |
| `toolchain_config.py` | 读 `config/toolchain.conf`，首次开箱时自动扫描 | 判断标准是「按配置里的路径能不能真的找到那个 exe」；进程内只扫一次 |
| `compiler_scanner.py` | 扫全盘找 GCC/Clang/MSVC，探测版本、头文件、gdb 是否带 Python；判「可用」与否，必要时补装 MinGW-w64 | 原来是个独立工具，现在是包内模块；TUI 由 `--scan-compiler` 调起。**「可用」= 编译出来的程序 GDB 能读到 DWARF**：MSVC（PDB）、无 gdb、gdb 不带 Python 都判不可用，只占位不自动选中。补装包版本写死（`TOOLCHAIN_PACKAGE`），换版本前先跑一遍 `tests/test_e2e.py` —— gdb 18.1 改了 sections/mappings 的输出格式，会让「段布局」和「内存区域分区」两块变空。**加速只在大陆且用户同意时用**，节点是 gh-proxy.org（第三方公共反代），界面上必须写明来源，`CDBG_NO_ACCELERATE=1` 可强制直连 |
| `instrument.py` | 往源码插一行 `#include "cdbg_shim.h"` | 有失败降级逻辑；插桩只记录、不改变内存布局 |
| `collector.py` | 探针数据 + memmap + heap ledger → Snapshot | 四个教学分区在这里拼装 |
| `snapshot.py` | 全部 dataclass 与 JSON 序列化 | **前后端唯一契约**，改字段必须同步前端渲染 |
| `memmap.py` | 平台门面：Windows `VirtualQueryEx` / Linux `/proc/<pid>/maps` | 新平台只需加一个 `memmap_<platform>.py`；`collector.py` 不要出现平台分支 |
| `heap_ledger.py` | 解析 shim 打出的堆账本 | 记录了每块内存是哪一行 malloc 的 |
| `curriculum.py` | 教学提示文案的唯一来源 | 改提示文案来这里，别散落到前端 |
| `web_server.py` | 静态托管 + REST + SSE + 操作日志接口 | 标准库 `ThreadingHTTPServer`，别加第三方框架 |
| `oplogger.py` | 操作日志落盘 | 观测者，任何异常都必须静默 |
| `quips.py` | 启动 / 底部小字的词库（来自上游 MIT 词库包，已删改） | 纯装饰，不许参与任何业务判断；`fill()` 负责替换 `{file_name}` 这类占位符；`/api/quips` 一次给全量，前端本地随机 |
| `browser.py` | 开浏览器 | — |

### 前端 `web/`（纯静态，无构建步骤）

每个模块是一个 IIFE，导出一个全局对象，加载顺序写在 `index.html` 底部：

`Addr → MemView → SourceView → VarPanel → Timeline → splitter.js → oplog.js → app.js`

| 对象 | 文件 | 主要接口 |
|---|---|---|
| `Addr` | `addr.js` | `load()` / `set()`，地址显示三种模式 |
| `MemView` | `memview.js` | `render(snap)` / `redrawPointers()`，HEX 网格、变量着色、指针连线 |
| `SourceView` | `sourceview.js` | `render()` / `setBreakpoints()` / `setErrors()` / `retargetBreakpoint()` / `setCursor()` / `getCursorLine()` / `setOnToggleBreak()` |
| `VarPanel` | `varpanel.js` | `renderStack()` / `renderVars()` / `renderLessons()` |
| `Timeline` | `timeline.js` | `push()` / `bind()` / `setOnView()`，快照回看与变量 diff |
| `app.js` | — | 状态中枢：`state` 在 IIFE 内，外部拿不到；所有改动从 `sendCommand()` 和 `updateChrome()` 入口进 |

`app.js` 里的两个关键函数：
- `sendCommand(cmd, extra)` — 所有调试命令的唯一出口，管 busy 状态和按钮禁用；
- `updateButtonStates()` — 根据 phase / busy / 有无快照决定 8 个按钮的可用性。

新增按钮要同时改：`index.html`（元素）、`app.js`（`bind()` 绑定 + `updateButtonStates()`）、`web/oplog.js` 的 `TOOLBAR_IDS`（否则不记日志）。

源码行的三种底色是**有语义**的，别随手加第四种：当前执行行 `--accent-soft`（蓝）、光标/运行到光标的行 `--cursor-soft`（绿，左侧 3px 实边）、鼠标划过的行 `--cursor-hover`（同色系更淡的绿）。划过之所以也用绿，是因为悬停本身就在暗示"点下去就成了光标行"；以前用中性的灰蓝，看着像第三种状态。

---

## 5. 契约与时序

**Snapshot 是唯一契约。** 采集端产出 dataclass，经 `to_json()` 变成前端认的 JSON。加字段时：

1. `snapshot.py` 的 dataclass 加字段；
2. `collector.py` 填值；
3. 前端对应模块的 `render()` 里消费。

漏任何一步都会静默丢数据（前端不会报错，只是空着）。

**一次「单步」的完整链路**（排查问题时按这个顺序打断点）：

```text
点击按钮 → app.js sendCommand("next")
  → POST /api/command {cmd:"next"}
  → web_server._Handler.do_POST → session.command()
  → GdbSession 发 "-exec-next"
  → GDB 停稳，gdb_probe 写快照 JSON 文件
  → GdbSession 检测到文件更新，Collector 聚合成 Snapshot
  → session._broadcast() 通知订阅者
  → SSE /api/events 推送 → 前端 es.onmessage → applySnapshot()
  → 各模块 render() + updateChrome() + Timeline.push()
```

`/api/state` 是首屏兜底，`/api/events` 是持续推送。`phase` 字段由后端在 SSE 里补上，前端据此决定按钮状态——**别在前端猜 phase**。

**`snapshot` 可以是 `null`。** 两个时刻会出现：刚启动（还没采过）和刚执行过 `clear`（清空界面）。SSE 会推 `data: null`，前端 `es.onmessage` 收到 null 就去拉 `/api/state`，`fetchState` 看到 `snapshot` 为空就调 `clearViewUI()` 把各视图渲染成空——**这条路径必须保持可用**，它是「清空」和「首屏没数据」共用的唯一入口。

### 源码插桩安全吗

堆跟踪会在源码里插入**一行** `#include "cdbg_shim.h"`，用户代码一行不改。安全上有五重保障：

1. shim 头里**先 include 标准库、再定义宏** —— 不会被宏污染到声明
2. 插入点选在**最后一个 `#include` 之后** —— 标准库头都已加载完毕
3. 检测到用户**自己定义了 malloc / free** 时直接放弃插桩
4. 插桩后**编译失败会自动降级**为不插桩重编一次
5. 插桩只做「记录」，**不改变任何内存布局或执行顺序**

---

## 6. 改代码的硬规矩

下面几条违反了不会立刻报错，但会埋下难查的回归，动手前先看一眼第 3、5、7 节：

1. **Snapshot 是前后端唯一契约。** 加字段要同时改三处：`snapshot.py` 的 dataclass → `collector.py` 填值 → 前端对应模块的 `render()` 消费。漏一步会静默丢数据。
2. **执行控制和采集是两条通道，别合并。** MI2 走 `gdb_session.py`，采集走注入的 `gdb_probe.py` + 约定快照文件；两边只靠"文件更新"这个信号同步。混用 GDB console 命令会让输出交错。
3. **新增按钮要改三处**：`index.html`（元素）、`app.js`（`bind()` 绑定 + `updateButtonStates()`）、`web/oplog.js` 的 `TOOLBAR_IDS`（否则操作日志不记这个按钮）。
4. **别在前端猜 `phase`**，它由后端在 SSE 里补；`snapshot` 可能是 `null`（首屏、清空后），这条路径必须保持可用。
5. **打包只能用 standalone，不能用 onefile。** onefile 把资源解压到 `%TEMP%\onefile_xxx`，而源码用 `__file__` 定位 `config/web/shim`，路径全乱、前端打不开。原因详见 `tools/build_standalone.py` 顶部注释。
6. **配置里存绝对路径**，判断"能不能用"必须落到 `os.path.isfile`，不能只看文件在不在。

---

## 7. 已知的坑（代码里都绕过了，别踩回去）

- **MI 设置不能带 `set` 前缀**：`-gdb-set pagination off` 对，`-gdb-set set pagination off` 错（静默失败，曾经导致程序窗口藏不起来）。
- **必须开 `mi-async`**：不开的话程序一 `continue`，GDB 就卡在 `wait_for_inferior` 里不再读 stdin；后面发的命令**一个字都不回**（实测 `-exec-interrupt`、`console kill` 6 秒无响应）。表现就是「停止按钮失灵 → 接着点调试也起不来 → 只能重启」。已写进 `_GDB_SETTINGS`。
- **停程序要「先 interrupt 再 kill」**：程序跑着时直接 `console kill` 会回 `^error ... ContinueDebugEvent failed (error 87)`；`-exec-interrupt`（回 `*stopped signal-received`）之后再 kill 才立刻回 `=thread-group-exited`。`GdbSession.kill()` 封装了这个顺序。
- **杀掉程序后快照文件还会再写一次**：探针是「中断」那一下开始采集的，写完可能已是下一条命令之后。直接 `sync_seq()` 会读到旧序号，于是下一次 `debug` 立刻拿到那份「程序已退出」快照，界面误报「已退出」。用 `sync_settled()`（盯 seq 直到稳定）记序号，另外 `_collect_locked` 里还有一道「事件说停住了、快照却说退出了 → 再等一份」的兜底。
- **编译必须 `-fno-inline`**：开了内联，单步时行号会跳，教学体验直接崩。
- **GDB 中文路径崩溃**：见第 3 节，源码必须先进 ASCII 目录。
- **断点会吸附**：`break file:18` 实际落在第 18 行之后第一个有机器码的位置。空行、纯声明行会被跳过。界面显示的是 GDB 回报的真实落点 `bkpt.line`，不是用户点的行——`retargetBreakpoint()` 就是干这个的。
- **取消断点必须发命令，不能只改本地标记**：`SourceView.toggleBreak()` 会先翻转本地 `Set` 让红点立刻消失，但**后端得另外收到 `{cmd:"break", on:false}`**。只改本地的话 B 边还留着断点，下一条命令触发的 `fetchState` 又把红点刷回来 —— 表现就是「删了又自己长回来」。`app.js` 里 `setOnToggleBreak` 的回调两个方向都要发。同理，`sendCommand` 因 busy 丢弃命令时要补一次 `fetchState`，把乐观更新的界面拉回真实状态。
- **同一行重复插入会留下两个断点**：GDB 允许同一位置挂多个断点，`_user_breakpoints` 是 `行号 → 断点号`，第二个会把第一个的号覆盖掉 —— 于是「删一次还剩一个」。`set_line_breakpoint()` 里加了两道去重（插入前查、吸附撞车后再查），多出来的那个立刻 `-break-delete`。
- **Windows 地址顺序和教材相反**：代码段在高位（`0x7FF6…`），栈在低位（`0x5FF8…`）。界面按**教材逻辑顺序**排列区块，但标注真实地址。
- **`%p` 和 GDB 地址格式不同**：前者 16 位大写无 `0x`，后者小写带 `0x`。统一在 `snapshot.format_address*()`。
- **函数里的 `static` 不在 GDB 的 global block**：只能从当前栈帧的可见变量里认，再按地址归到 `.data` / `.bss`。
- **被调试程序有独立控制台**：不能和 MI 共用 stdin，否则 `scanf` 会读到调试命令或挂死。
- **未初始化局部变量**：值可能是栈残留垃圾，界面要明确标「尚未初始化」；全局变量才是 0。
- **Dev-C++ 自带的 gdb 没有 Python**：实测 `C:\Program Files (x86)\Dev-Cpp\MinGW32\bin\gdb.exe` 会回「Python scripting is not supported in this copy of GDB」。选它当调试器，程序能编能跑，但 `gdb_probe.py` 加载失败 → 界面一个变量都没有。扫描时已按 `gdb的Python支持` 排序把它们排后面。
- **配置里存的是绝对路径**：换设备、卸载重装、改盘符都会让它失效。判断「能不能用」必须落到 `os.path.isfile`，不能只看配置存在 —— 否则会拿一个不存在的路径去 `Popen`，报错信息离真正原因十万八千里。
- **「清空界面」必须清在会话层，不能只擦 DOM**：画面是 `/api/state` + SSE 的投影，前端擦一遍之后，任何一次 `fetchState`（下一条命令的 finally、刷新页面）都会把旧快照请回来。 `session._clear_view()` 干三件事：程序还在跑就先 kill、`snapshot = None`、`state_name = "not-started"`，再 `_broadcast(None)`。**断点刻意不清**（它属于源码上的设置）。这个分支必须在 `command()` 里**提前 return**，否则后面的 `_collect_and_broadcast` 会立刻把刚扔掉的快照捡回来。
- **清空界面时 `render(source, null)` 会保留上一行的 `currentLine`**：这是刻意的（重画源码时不希望高亮乱跳），所以清空要先调 `SourceView.clearPosition()` 把蓝行/绿行显式抹掉。
- **长按强制结束只杀 inferior，不杀 gdb**：Windows 用 `taskkill /PID <pid> /F`，Linux 用 `SIGKILL` 干掉被调试程序，GDB 收到退出事件后自己醒过来，符号表和断点都还在，接着按「调试」能直接重来。PID 从快照里抄（`session._target_pid`），**每次起新 inferior 都要清空**：Windows 会复用 PID，拿旧 PID 开枪可能打到无关进程。
- **`taskkill` 的输出不要 `text=True` 收**：中文系统上是 GBK，换区域设置就会解码报错；只关心退出码，stdout/stderr 一律 `DEVNULL`，并带 `CREATE_NO_WINDOW` 免得闪黑框。
- **「停止执行」的点击不能挂在 `click` 上**：长按 1 秒后松手，`click` 也会跟着触发，等于强杀完又发一条 stop。改成 `pointerdown` 起计时、`pointerup` 收尾（挂在 `window` 上，手指拖出按钮再松手也要收尾，否则进度条会一直挂着），不满 1 秒才走普通 `stop`。
- **「停止 / 重启」永远不灰，也不排队**：程序卡在 `scanf`、或某条命令一直等不到结果时，它们是唯一的出路。前端 `updateButtonStates` 对这两颗无条件 `enabled`、`setControlsDisabled` 的名单里也没有它们，`sendCommand` 用 `URGENT_CMDS` 绕过 `state.busy` 队列；后端 `command()` 里 stop / restart / force-kill / clear 在 `_exec_lock` **之前**就返回，并且先 `_abort_wait.set()` 把正在等待的命令叫停。
- **等程序停下的那几秒不能抱着 `_lock`**：以前 `command()` 整个方法（含 8 秒等待）都在 `with self._lock` 里，于是在 `scanf` 上按「跳出函数」时，停止按钮、重启按钮、连 `/api/state` 全被堵住（日志里那次 11 秒的 finish 就是这么来的）。现在 `_lock` 只护状态字段、`_exec_lock` 只串行化执行类命令，等待发生在两把锁外面。
- **`_abort_wait` 是「一次性的叫停信号」**：`_wait_stopped` / `_wait_new_snapshot` 按 0.2 秒一片轮询它，所以停止能在一个 tick 内生效。但**监视线程的采集必须 `abortable=False`** —— 它是一次已经过去的中止信号，误伤它就会让「重启之后新程序的快照永远等不到」（表现为重启完界面一直显示「运行中」、行号停在旧位置）。
- **跳出函数要先看当前栈帧**：`-exec-finish` 的语义是「跑到当前函数返回」，停在 `scanf` 这类系统函数里时它得等你输入才返回，一条命令能耗掉 11 秒。`session._finish_guard()` 在发给 GDB **之前**判断：当前帧不是用户代码（`_is_user_frame`：GDB 常把库源码报成光秃秃的 `ucrt_scanf.c`，或落在 mingw/ucrt/include 目录下）、或者已经停在 `main`，就直接回一句话，命令不发出去。
- **徽章上的临时提示要「占位」**：`setBadge()` 只负责写文字，而 `updateChrome()` 随时会被 SSE 快照刷新调用 —— 不占位的话，提示只活到下一帧（实测 200ms 就被「就绪」盖掉）。现在 `setBadge` 记一个 `badgeHold = {text, until}`（默认 2 秒），`updateChrome` 在到期前不碰徽章；同一句话正在显示时直接 return，连点几下也不会把计时重置成「永远显示」。
- **底部小字：词库写死在 Python 里**（`cdbg/quips.py`）。打包成单文件 exe 之后没有「源码目录」，为几行字再加一条资源定位和编码处理不值得；前端通过 `/api/quips` 一次拿全量，本地随机，不为换条字发请求。点击监听挂在 `document` 的**捕获阶段**，按钮自己的 handler 就算 `stopPropagation` 也挡不住换词。「隐藏」只改 DOM，不写 `localStorage` —— 它是「这一次」的事，刷新就该回来。
- **文案里的占位符要填掉**：上游词库带 `{file_name}` / `{provider}`，不替换的话界面上会明晃晃显示花括号，看着像我们自己的 bug（`quips.fill()`）。
- **「程序输出」面板只能藏，不能删**：`bindConsole()` / `pollConsole()` / `setConsoleHint()` 和输入表单的 submit 都盯着 `console-panel` / `console-output` / `console-hint` / `console-input` / `console-form` 这几个 id；`pollConsole()` 拿不到 `console-output` 会**直接 return**（静默失效），Linux 上就再也没法给 `scanf` 喂输入了。现在是「HTML 里默认 `hidden`，`setConsoleHint(mode)` 里 `mode === "pty"` 才亮出来」。
- **`.panel` 自带 `display: flex`，会盖掉 `[hidden]` 的 `display: none`**（作者样式优先于 UA 样式）。所以 `theme.css` 里必须有一条 `.panel-console[hidden] { display: none; }`，否则「藏起来」的面板照样占着整行高度。
- **面板显隐看后端 `mode`，别看 UA**：Windows 上是 `own`/`shared`（程序有自己的控制台窗口），Linux 是 `pty`。WSL 里 cdbg 跑在 Linux、浏览器却在 Windows，按 `navigator.userAgent` 藏了就把人家唯一的输入口藏没了。

---

## 8. 测试与调试工作流

```bash
python tests/test_e2e.py              # 端到端冒烟：编译 → 起 GDB → 采集 → 单步
python tests/test_batch.py            # 批量测 examples/
python tests/test_batch.py D:/cdesign # 批量测任意目录（自带跳过第三方老代码的逻辑）
python tests/test_examples.py         # 示例集
python tests/test_memmap_linux.py     # /proc/<pid>/maps 解析（平台无关，任何系统都能跑）

# Linux / WSL2（在 ~/cdbg 这类 ext4 目录里跑）
python3 tests/test_memmap_linux.py
python3 tests/test_examples.py
python3 tests/test_e2e.py
```

Linux 上交互式程序（`ch10_input.c`）通过 PTY 运行，输出和输入都在 Web 的
「程序输出」面板里；非交互示例的行为与 Windows 一致。

批量测试对每个源文件走四关：**编译 → 启动 → 停在 main → 采集到变量信息**。源程序本身有编译错误时，只要工具能定位到出错位置（文件/行号/原因），就算它工作正常 —— 源程序的错不是工具的错。

前端 UI 回归用现成的截图脚本（在 `tools/` 下跑，需先起一个 `--no-browser --port 8779` 的实例）：

```bash
node shot.js http://127.0.0.1:8779/ shot.png btn-debug:2 btn-next:1
node ui_check.js http://127.0.0.1:8779/           # 断点开关全流程 + 三种行底色，出两张图
node ui_clear_test.js http://127.0.0.1:8779/     # 清空界面（含刷新后不复活）+ 长按停止
node ui_forcekill_test.js http://127.0.0.1:8779/ # 程序卡在 scanf 时长按强制结束（配 ch10_input.c）
```

`ui_check.js` 按 2560×1600 @150% 的真机视口（CSS 1707×1067、DPR 1.5）跑，会依次点同一行的「设→取消」、设两个再取消一个、点空行看吸附，每一步都同时对比**界面红点**和 `/api/state` 里的 `breakpoints` —— 两者不一致就是又出现「删了又长回来」的回归了。`ui_clear_test.js` 会在清空后 `reload()` 一次：清空如果只擦 DOM，刷新就会把旧快照带回来，这条断言就是专门盯这个的。

排查编译问题时用 `--keep-workdir` 保留临时目录；排查状态时用 `--headless` 直接看终端快照。日志默认被 `_Handler.log_message` 静默掉了，要调试 HTTP 就临时打开它。

---

## 9. 操作记录器

**目的**：记录用户的输入操作，落盘到 `log/oplog_<启动时刻>.log`，供事后复盘。

**设计原则：纯旁路，不能干扰调试器。** 它出错时主程序必须照常工作。

- `cdbg/oplogger.py` — 一次启动一个文件，文件名带毫秒时间戳；路径从 `__file__` 定位，与 cwd 无关；读写共用一把锁；所有写盘异常静默。
- `web/oplog.js` — 观测层，**不改任何现有处理函数**：
  - 按钮点击 → 捕获阶段 `document.addEventListener('click', ..., true)`；
  - 按钮禁用/启用 → 对 8 个上排按钮挂 `MutationObserver(attributeFilter:['disabled'])`；
  - busy 进退 → 包装 `window.fetch`，盯 `/api/command` 的生命周期（`state.busy` 在 IIFE 里拿不到，但两者严格同生命周期），原样返回同一个 Promise；
  - 加载文件 → 观察 `#prog-name` 文本变化；
  - 写日志用 `sendBeacon`，失败退回 `fetch(keepalive)`，全程 `try/catch`。
- `web_server.py` — `POST /api/oplog` 收事件（放在 session 检查之前，永远回 `ok`）；`GET /api/oplog` 供查看器读取。
- `web/oplog.html` — 独立查看页 `http://127.0.0.1:<端口>/oplog.html`，1.5 秒自动刷新，**不在主界面加任何按钮**。

**扩展方式**：前端想加新事件，直接 `emit('类型', '描述', {字段: 值})` 即可，后端会原样把字段追加到行尾。不要为了记日志去改 `app.js` 的业务逻辑。

事件类型：

| 类型 | 写的人 | 说明 |
|---|---|---|
| `SESSION` / `PAGE` / `FILE` | 后端 / 前端 | 启动、页面就绪、加载的文件 |
| `CLICK` / `KEY` / `INPUT` | 前端（捕获阶段） | 用户按了什么 |
| `BTN` | 前端（观察 disabled） | 上排按钮的启用/禁用变化，量最大，复盘时可过滤 |
| `BUSY` / `CMDRES` | 前端（包装 fetch） | 命令发出的瞬间 / 后端怎么回的（ok / waiting / killed / msg） |
| `LOC` | 前端（盯 `#status-loc`） | 程序停在哪：`文件:行号 函数` —— **每一步都有一行** |
| `PHASE` | 前端（盯 `#status-badge`）+ 后端 | 未执行 / 运行中 / 就绪 / 程序已退出，以及「继续运行」「停止执行」等动作 |
| `CMD` | 后端 | 每条命令的耗时、结果、`killed`、`waiting` |
| `SNAP` | 后端 | 每次采集完成：行号、函数、状态、seq、栈帧数 |
| `BREAK` | 后端 | 断点设置了哪一行、GDB 实际落在哪一行 |
| `WAIT` | 后端 | 没等到 `*stopped`（多半卡在 scanf 等人输入） |
| `ERROR` | 前端 | 页面异常 / 未处理的 Promise 拒绝 |

> 注意时间戳是**服务端收到的时刻**（前端只发 type/message/字段），`sendBeacon` 不保证到达顺序，所以同一毫秒内的事件顺序可能颠倒（例如后端写的 `PHASE` 排在 `CLICK` 前面）。

---

## 10. 云端构建与发布

工作流在 `.github/workflows/build.yml`，打包脚本在 `tools/build_standalone.py`。

**触发规则：**

| 方式 | 何时跑 |
|---|---|
| 手动 `workflow_dispatch` | 随时，去 Actions 页选 "Build cdbg (Windows standalone zip)" → Run workflow |
| 定时 `schedule` | 每天 UTC 17:00 = 北京时间次日凌晨 01:00 |
| 空跑保护 | 定时触发时对比 `last-built` tag 和当前 HEAD，没有新提交就跳过 |

**流程：** `check`（判断要不要构建）→ `build`（Windows runner + Python 3.14 + Nuitka + MSVC）→ 上传 artifact → 为每个新 commit 新建一个 `alpha-<commit 前 7 位>` 预发布 release → 打 `last-built` tag 记账。

每个 Release 的 tag 指向构建时的 commit，所以 zip 与 GitHub 自动生成的源码包出自同一份代码。不再反复覆盖固定的 `alpha` release（那样会出现 zip 是新的、源码包却是旧 tag 的情况）。同一个 commit 被手动重复构建时只刷新 zip，不新建 Release，也不覆盖 release 说明。

Windows runner 不是随便选的：Nuitka 生成 Windows PE exe 必须调用 MSVC 链接器，`windows-latest` 自带 MSVC 和 Windows SDK，Linux runner 做不到。

**产物：** `cdbg-windows-x64.zip`，解压即用。里面是 `cdbg.exe` + `cdbg/gdb_probe.py` + `web/` + `shim/` + `examples/` + 空 `config/` + `README.txt`。打包时会剔除本机 `toolchain.conf` 和 `log/`。

**本地复现云构建**（需要装了 Nuitka 的 `C:\Python314\python.exe`）：

```bash
python tools/build_standalone.py --output <pack下的独立目录> --msvc
```

本地构建产物按约定一律放到工作区的 `pack\` 下，**每次构建单独建一个文件夹**，不要污染源码目录；清理时分批手动删，别用批量删除（会触发风控）。

---

## 11. 提交与 PR

- 一个提交只做一件事，commit message 说清"改了什么、为什么"，别写 `update` 这种。
- 改了 `snapshot.py` 的字段，PR 描述里列出前后端同步改了哪几处。
- 动了 GDB 时序、断点、清空、强制结束这些逻辑，附上对应的 `ui_*.js` 回归结果。
- 不确定是不是回归的，先跑第 8 节全套测试再提。
- 实现了 [`TODO.md`](TODO.md) 里的某一项，记得从清单里删掉对应条目，并把关键决策补进本文档。

---

## 12. 上手建议

1. 先跑 `python tests/test_e2e.py` 确认工具链没问题；
2. 用 `--no-browser --port 8779` 启动，浏览器开界面，另开标签页开 `/oplog.html`；
3. 想看内存数据结构，在 `snapshot.py` 打断点（`collector.py` 是填值的地方）；
4. 想看执行控制，在 `session.command()` 和 `GdbSession` 的命令收发处打断点；
5. 动前端前先看 `app.js` 的 `state` 和 `updateChrome()`，绝大部分界面异常都从这两个地方查起。

---

## 13. 许可证与致谢

本项目用 **MIT License**（见 [`LICENSE`](LICENSE)）。提交 PR 即表示你同意自己的贡献也按 MIT 授权。

交互设计参考了 **Red Panda Dev C++（小熊猫 Dev-C++，royqh1979/RedPanda-CPP）**：快捷键语义、GDB 启动方式、工具链字段命名等，目的是让 C 语言初学者从 Dev-C++ 切过来不用重新学。上游项目以 **GPL v3** 发布（`Dev-CPP-master.zip` 内 `LICENSE` 为证）；本项目是独立的 Python/HTML 实现，**未包含其源码**，只参考了行为设计——所以这里用 MIT 授权，并在致谢里注明来源。

`Dev-CPP-master.zip` 那份源码包仅作参考，已进 `.gitignore`，不要提交进仓库。
