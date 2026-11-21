# DEVREADME · 给下一位维护者

`README.md` 是给**使用者**看的（怎么装、怎么操作、界面长什么样）。
这份是给**改代码的人**看的：架构、契约、坑在哪里、改一处会牵动哪里。

> **工作区里还有一个 `cdbg - 副本` 目录，那是备份，不要改它。** 所有开发都在本目录进行。

---

## 1. 环境（Windows 限定）

| 依赖 | 说明 |
|---|---|
| Python 3.9+ | 只用标准库，不要引入 Flask / FastAPI / pip 包 |
| GCC | 需 `-g -O0`，实测路径 `C:\msys64\ucrt64\bin\gcc.exe` |
| GDB | **必须带 Python 支持**：`gdb --batch -ex "python print(1)"` 能打印才算合格 |
| Edge | 截图/UI 验证用 `playwright-core`（已装在 `tools/node_modules`），走 `channel: "msedge"` |

工具链不再靠猜：**`config/toolchain.conf` 是唯一权威来源**（由
`compiler_scanner.py` 扫描生成，`toolchain_config.py` 读取）。

```bash
python cdbg.py --toolchain          # 看当前用的是哪个 gcc/gdb、配置在哪
python cdbg.py --scan-compiler      # 重扫 + TUI 选择（scan_compiler.bat 同效）
python cdbg.py --scan-compiler --select 2   # 非交互直选第 2 套
```

**首次开箱**（配置缺失 / 空 / 里面的路径在这台机器上找不到）会自动扫一遍并
覆盖配置，所以整个目录拷到别的设备也能直接跑。`CDBG_CC` / `CDBG_GDB` 仍可
临时覆盖，优先级高于配置。

**只有 Windows**：`memmap.py` 用 `VirtualQueryEx` 从进程外部查堆栈边界，Linux/macOS 直接跑不起来。

---

## 2. 跑起来

```bash
cd cdbg
python cdbg.py examples/ch06_pointer.c              # 编译 + 起 GDB + 起 Web + 开浏览器
python cdbg.py prog.c --no-browser --port 8779       # 自己访问 http://127.0.0.1:8779/
python cdbg.py prog.c --headless                     # 终端输出快照，给 IDE 调用
python cdbg.py prog.exe --keep-workdir               # 调试现成 exe，保留临时工作目录
python cdbg.py prog.c --no-instrument                # 关掉堆跟踪插桩
python cdbg.py --toolchain                           # 打印 gcc/gdb/配置路径
python cdbg.py --scan-compiler                       # 重扫本机编译器并交互选择
```

`cdbg.bat` / `start.bat` 是「打开方式」和拖拽入口，`scan_compiler.bat` 是工具链选择器，
内部就是 `python cdbg.py ...`，不 `cd` 到脚本目录——**别假设启动时的 cwd 是项目根目录**
（日志模块、工具链配置都特意从 `__file__` 定位路径）。

---

## 3. 架构：两条通道

整套东西的核心设计是**执行控制与数据采集分离**：

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

**为什么要这么绕**：MI 通道和 GDB console 解释器混用会让输出交错、解析错乱。所以采集走独立脚本 + 约定文件，两边只靠「快照文件更新」这个信号同步。改这一块之前先想清楚别把两条通道并回去。

另外，GDB 用 GBK 解码路径，中文目录会直接崩。所以源码先复制到**纯 ASCII 临时工作目录**再编译（`launcher.make_workspace`），界面显示的仍是原始路径（`_map_back` 映射回来）。

---

## 4. 模块地图

### 后端 `cdbg/`

| 文件 | 职责 | 改动时注意 |
|---|---|---|
| `session.py` | 会话编排：`start` / `command` / `shutdown`、快照发布、phase 管理 | **命令分发总入口**。新增调试动作先在 `command()` 加分支，再配 GdbSession 和前端按钮 |
| `gdb_session.py` | MI2 协议收发、`*stopped` 事件、等待快照文件 | 不要混用 console 命令；`_GDB_SETTINGS` 的项不能带 `set` 前缀 |
| `gdb_probe.py` | 跑在 **GDB 进程内**的 Python：变量、栈帧、字节、段边界 | 这里是 GDB 的 Python 2/3 环境，不是主进程的 Python；异常会静默，注意兜底 |
| `launcher.py` | 工具查找、ASCII 工作目录、编译、编译/链接错误诊断 | `DEBUG_FLAGS` 里每个 flag 都有教学含义，别随手删。**gcc/gdb 的解析入口在这里**（`find_compiler` / `find_gdb`），顺序是 环境变量 → 配置 → PATH → 常见目录 |
| `toolchain_config.py` | 读 `config/toolchain.conf`，首次开箱时自动扫描 | 判断标准是「按配置里的路径能不能真的找到那个 exe」；进程内只扫一次 |
| `compiler_scanner.py` | 扫全盘找 GCC/Clang/MSVC，探测版本、头文件、gdb 是否带 Python | 原来是个独立工具，现在是包内模块；TUI 由 `--scan-compiler` 调起 |
| `instrument.py` | 往源码插一行 `#include "cdbg_shim.h"` | 有失败降级逻辑；插桩只记录、不改变内存布局 |
| `collector.py` | 探针数据 + memmap + heap ledger → Snapshot | 四个教学分区在这里拼装 |
| `snapshot.py` | 全部 dataclass 与 JSON 序列化 | **前后端唯一契约**，改字段必须同步前端渲染 |
| `memmap.py` | `VirtualQueryEx` 查真实堆栈边界 | Windows 专用；GDB 在 Windows 拿不到 `info proc mappings` |
| `heap_ledger.py` | 解析 shim 打出的堆账本 | 记录了每块内存是哪一行 malloc 的 |
| `curriculum.py` | 教学提示文案的唯一来源 | 改提示文案来这里，别散落到前端 |
| `web_server.py` | 静态托管 + REST + SSE + 操作日志接口 | 标准库 `ThreadingHTTPServer`，别加第三方框架 |
| `oplogger.py` | 操作日志落盘 | 观测者，任何异常都必须静默 |
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

源码行的三种底色是**有语义**的，别随手加第四种：当前执行行 `--accent-soft`（蓝）、光标/运行到光标的行
`--cursor-soft`（绿，左侧 3px 实边）、鼠标划过的行 `--cursor-hover`（同色系更淡的绿）。划过之所以也用绿，
是因为悬停本身就在暗示「点下去就成了光标行」；以前用中性的灰蓝，看着像第三种状态。

---

## 5. 契约与时序

**Snapshot 是唯一契约。** 采集端产出 dataclass，经 `to_json()` 变成前端认的 JSON。加字段时：

1. `snapshot.py` 的 dataclass 加字段；
2. `collector.py` 填值；
3. 前端对应模块的 `render()` 里消费。

漏任何一步都会静默丢数据（前端不会报错，只是空着）。

**一次「单步」的完整链路**（排查问题时按这个顺序打断点）：

```
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

**`snapshot` 可以是 `null`。** 两个时刻会出现：刚启动（还没采过）和刚执行过 `clear`
（清空界面）。SSE 会推 `data: null`，前端 `es.onmessage` 收到 null 就去拉 `/api/state`，
`fetchState` 看到 `snapshot` 为空就调 `clearViewUI()` 把各视图渲染成空——**这条路径必须
保持可用**，它是「清空」和「首屏没数据」共用的唯一入口。

---

## 6. 已知的坑（代码里都绕过了，别踩回去）

- **MI 设置不能带 `set` 前缀**：`-gdb-set pagination off` 对，`-gdb-set set pagination off` 错（静默失败，曾经导致程序窗口藏不起来）。
- **必须开 `mi-async`**：不开的话程序一 `continue`，GDB 就卡在 `wait_for_inferior` 里不再读 stdin；
  后面发的命令**一个字都不回**（实测 `-exec-interrupt`、`console kill` 6 秒无响应）。
  表现就是「停止按钮失灵 → 接着点调试也起不来 → 只能重启」。已写进 `_GDB_SETTINGS`。
- **停程序要「先 interrupt 再 kill」**：程序跑着时直接 `console kill` 会回
  `^error ... ContinueDebugEvent failed (error 87)`；`-exec-interrupt`（回 `*stopped signal-received`）
  之后再 kill 才立刻回 `=thread-group-exited`。`GdbSession.kill()` 封装了这个顺序。
- **杀掉程序后快照文件还会再写一次**：探针是「中断」那一下开始采集的，写完可能已经是下一条命令之后。
  直接 `sync_seq()` 会读到旧序号，于是下一次 `debug` 立刻拿到那份「程序已退出」的快照，
  界面误报「已退出」。用 `sync_settled()`（盯 seq 直到稳定）记序号，另外 `_collect_locked`
  里还有一道「事件说停住了、快照却说退出了 → 再等一份」的兜底。- **编译必须 `-fno-inline`**：开了内联，单步时行号会跳，教学体验直接崩。
- **GDB 中文路径崩溃**：见第 3 节，源码必须先进 ASCII 目录。
- **断点会吸附**：`break file:18` 实际落在第 18 行之后第一个有机器码的位置。空行、纯声明行会被跳过。界面显示的是 GDB 回报的真实落点 `bkpt.line`，不是用户点的行——`retargetBreakpoint()` 就是干这个的。
- **取消断点必须发命令，不能只改本地标记**：`SourceView.toggleBreak()` 会先翻转本地 `Set`
   让红点立刻消失，但**后端得另外收到 `{cmd:"break", on:false}`**。只改本地的话 B 边还留着断点，
   下一条命令触发的 `fetchState` 又把红点刷回来 —— 表现就是「删了又自己长回来」「再设一个断点，旧的复活了」。
   `app.js` 里 `setOnToggleBreak` 的回调两个方向都要发。同理，`sendCommand` 因 busy 丢弃命令时要补一次
   `fetchState`，把乐观更新的界面拉回真实状态。
- **同一行重复插入会留下两个断点**：GDB 允许同一位置挂多个断点，`_user_breakpoints` 是 `行号 → 断点号`，
  第二个会把第一个的号覆盖掉 —— 于是「删一次还剩一个」，程序照样停在那儿。`set_line_breakpoint()`
   里加了两道去重（插入前查、吸附撞车后再查），多出来的那个立刻 `-break-delete`。
- **Windows 地址顺序和教材相反**：代码段在高位（`0x7FF6…`），栈在低位（`0x5FF8…`）。界面按**教材逻辑顺序**排列区块，但标注真实地址。
- **`%p` 和 GDB 地址格式不同**：前者 16 位大写无 `0x`，后者小写带 `0x`。统一在 `snapshot.format_address*()`。
- **函数里的 `static` 不在 GDB 的 global block**：只能从当前栈帧的可见变量里认，再按地址归到 `.data` / `.bss`。
- **被调试程序有独立控制台**：不能和 MI 共用 stdin，否则 `scanf` 会读到调试命令或挂死。
- **未初始化局部变量**：值可能是栈残留垃圾，界面要明确标「尚未初始化」；全局变量才是 0。
- **Dev-C++ 自带的 gdb 没有 Python**：实测 `C:\Program Files (x86)\Dev-Cpp\MinGW32\bin\gdb.exe`
  会回「Python scripting is not supported in this copy of GDB」。选它当调试器，程序能编能跑，
  但 `gdb_probe.py` 加载失败 → 界面一个变量都没有。扫描时已按 `gdb的Python支持` 排序把它们排后面。
- **配置里存的是绝对路径**：换设备、卸载重装、改盘符都会让它失效。判断「能不能用」必须
  落到 `os.path.isfile`，不能只看配置存在 —— 否则会拿一个不存在的路径去 `Popen`，报错信息
  离真正原因十万八千里。
- **「清空界面」必须清在会话层，不能只擦 DOM**：画面是 `/api/state` + SSE 的投影，前端擦一遍
  之后，任何一次 `fetchState`（下一条命令的 finally、刷新页面）都会把旧快照请回来 —— 那就是
  「假清空」。`session._clear_view()` 干三件事：程序还在跑就先 kill、`snapshot = None`、
  `state_name = "not-started"`，再 `_broadcast(None)`。**断点刻意不清**（它属于源码上的设置）。
  这个分支必须在 `command()` 里**提前 return**，否则后面的 `_collect_and_broadcast` 会立刻把刚扔掉
  的快照捡回来。
- **清空界面时 `render(source, null)` 会保留上一行的 `currentLine`**：这是刻意的（重画源码时
  不希望高亮乱跳），所以清空要先调 `SourceView.clearPosition()` 把蓝行/绿行显式抹掉。
- **长按强制结束只杀 inferior，不杀 gdb.exe**：`taskkill /PID <pid> /F` 干掉被调试程序，
  GDB 收到退出事件后自己醒过来，符号表和断点都还在，接着按「调试」能直接重来 —— 杀掉 gdb.exe
  就要重编译重连，代价太大。PID 从快照里抄（`session._target_pid`），**每次起新 inferior 都要
  清空**：Windows 会复用 PID，拿旧 PID 开枪可能打到无关进程。
- **`taskkill` 的输出不要 `text=True` 收**：中文系统上是 GBK，换区域设置就会解码报错；只关心
  退出码，stdout/stderr 一律 `DEVNULL`，并带 `CREATE_NO_WINDOW` 免得闪黑框。
- **「停止执行」的点击不能挂在 `click` 上**：长按 3 秒后松手，`click` 也会跟着触发，等于强杀完
  又发一条 stop。改成 `pointerdown` 起计时、`pointerup` 收尾（挂在 `window` 上，手指拖出按钮再
  松手也要收尾，否则进度条会一直挂着），不满 3 秒才走普通 `stop`。

---

## 7. 测试与调试工作流

```bash
python tests/test_e2e.py              # 端到端冒烟：编译 → 起 GDB → 采集 → 单步
python tests/test_batch.py            # 批量测 examples/
python tests/test_batch.py D:/cdesign # 批量测任意目录（自带跳过第三方老代码的逻辑）
python tests/test_examples.py         # 示例集
```

前端 UI 回归用现成的截图脚本（会打印页面元素统计和 console 错误）：

```bash
cd tools
node shot.js http://127.0.0.1:8779/ shot.png btn-debug:2 btn-next:1
node ui_check.js http://127.0.0.1:8779/           # 断点开关全流程 + 三种行底色，出两张图
node ui_clear_test.js http://127.0.0.1:8779/     # 清空界面（含刷新后不复活）+ 长按停止
node ui_forcekill_test.js http://127.0.0.1:8779/ # 程序卡在 scanf 时长按强制结束（配 ch10_input.c）
```

`ui_check.js` 按 2560×1600 @150% 的真机视口（CSS 1707×1067、DPR 1.5）跑，会依次点同一行的
「设→取消」、设两个再取消一个、点空行看吸附，每一步都同时对比**界面红点**和 `/api/state`
里的 `breakpoints` —— 两者不一致就是又出现「删了又长回来」的回归了。
`ui_clear_test.js` 会在清空后 `reload()` 一次：清空如果只擦 DOM，刷新就会把旧快照带回来，
这条断言就是专门盯这个的。

排查编译问题时，`--keep-workdir` 保留临时目录；排查状态时 `--headless` 直接看终端快照。日志默认被 `_Handler.log_message` 静默掉了，要调试 HTTP 就临时打开它。

---

## 8. 操作记录器（新增于 2026-11-20）

**目的**：记录用户的输入操作，落盘到 `log/oplog_<启动时刻>.log`，供事后复盘。

**设计原则：纯旁路，不能干扰调试器。** 它出错时主程序必须照常工作。

- `cdbg/oplogger.py` — 一次启动一个文件，文件名带毫秒时间戳；路径从 `__file__` 定位，与 cwd 无关；读写共用一把锁；所有写盘异常静默。
- `web/oplog.js` — 观测层，**不改任何现有处理函数**：
  - 按钮点击 → 捕获阶段 `document.addEventListener('click', ..., true)`；
  - 按钮禁用/启用 → 对 8 个上排按钮挂 `MutationObserver(attributeFilter:['disabled'])`；
  - busy 进退 → 包装 `window.fetch`，盯 `/api/command` 的生命周期（`state.busy` 在 IIFE 里拿不到，但两者严格同生命周期），原样返回同一个 Promise；
  - 加载文件 → 观察 `#prog-name` 文本变化。
  - 写日志用 `sendBeacon`，失败退回 `fetch(keepalive)`，全程 `try/catch`。
- `web_server.py` — `POST /api/oplog` 收事件（放在 session 检查之前，永远回 `ok`）；`GET /api/oplog` 供查看器读取。
- `web/oplog.html` — 独立查看页 `http://127.0.0.1:<端口>/oplog.html`，1.5 秒自动刷新，**不在主界面加任何按钮**。

**扩展方式**：前端想加新事件，直接 `emit('类型', '描述', {字段: 值})` 即可，后端会原样把字段追加到行尾。不要为了记日志去改 `app.js` 的业务逻辑。

事件类型（2026-11-20 补全，专门为了查「停止 / 重启」这类问题）：

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

> 注意时间戳是**服务端收到的时刻**（前端只发 type/message/字段），`sendBeacon` 不保证到达顺序，
> 所以同一毫秒内的事件顺序可能颠倒（例如后端写的 `PHASE` 排在 `CLICK` 前面）。

---

## 9. 上手建议

1. 先跑 `python tests/test_e2e.py` 确认工具链没问题；
2. 用 `--no-browser --port 8779` 启动，浏览器开界面，另开标签页开 `/oplog.html`；
3. 想看内存数据结构，在 `snapshot.py` 打断点（`collector.py` 是填值的地方）；
4. 想看执行控制，在 `session.command()` 和 `GdbSession` 的命令收发处打断点；
5. 动前端前先看 `app.js` 的 `state` 和 `updateChrome()`，绝大部分界面异常都从这两个地方查起。

---

## 10. TODO：双运行时发布 + 编译器不可用时的自动补装

> 还没实现，这里是留给下一位维护者的设计备忘。许可证维持 MIT，不要改。

**背景**：当前云编译只产出不带编译器的免安装包（`tools/build_standalone.py`），
运行时靠 `compiler_scanner.py` 扫本机的 gcc/gdb。这有两个问题：

1. 新手机器上可能一个 gcc 都没有 → 需要 **with runtime** 包（内置一套 MinGW-w64）。
2. 装了 Visual Studio 的机器会扫到 `cl.exe`（MSVC）。`compiler_scanner.py` 目前
   把它当合法候选（`_detect_family` 认 `cl.exe`，第 408、424 行），但 **MSVC 产出
   PDB 调试信息，GDB 只认 DWARF**（且有 GCC Runtime Library Exception 的区别）。
   选了 MSVC 编译出来的 exe 放进 cdbg：能编译、能启动，但变量/行号/调用栈全空。
   这不是"不支持某些特性"，是调试信息格式根本对不上。

**要做的事**：

- 发布两种包：`cdbg-windows-x64-with-runtime.zip`（gcc + gdb 一起打进去，
  解压即用）和 `cdbg-windows-x64.zip`（保持现状）。共用同一套构建脚本，
  用参数切换是否 include 工具链目录；CI 里两个 artifact 都上传。
- 工具链目录里 **gdb 必须带 Python 支持**（`gdb --batch -ex "python print(1)"`
  能打印才算合格），否则 `gdb_probe.py` 加载不了，界面一个变量都没有。
- without runtime 版增加"编译器不可用"判定：扫到的候选里只有 MSVC、或者
  gdb 不带 Python 支持时，不要直接拿它编译，而是进入补装流程。
- 补装流程：拉取一份 MinGW-w64（gcc + gdb）到本地固定目录
  （建议 `%LOCALAPPDATA%\cdbg\toolchain\`，别写进程序目录，避免 UAC 和
  每次解压被覆盖），校验哈希，然后调用现有的 `write_config` 把路径写进
  `config/toolchain.conf`，下次启动直接复用。要考虑国内下载慢 → 支持镜像源、
  断点续传、失败重试；下载控件不要引第三方库（本项目坚持纯标准库）。
- `_detect_family` 对 MSVC 的处理要重新想：要么直接把它从候选里摘掉，
  要么只在"没有别的可用编译器"时留作占位并触发挥发补装，别让它被静默选中。
- **许可证义务**：内置 gcc/gdb 是 GPL v3 程序的分发，属于 mere aggregation，
  本项目 MIT 不受影响；但 with runtime 包里必须附带 gcc/gdb 的 GPL 文本、
  版权声明和**对应源码的获取途径**（官方源码 URL + 具体版本号即可），
  学生的 `.c` 经 gcc 编译出的程序受 GCC Runtime Library Exception 保护，不用开源。
