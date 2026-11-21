# CONTRIBUTING · 接手与贡献指南

这份是**接手入口**：怎么把项目跑起来、怎么改、改完怎么验证、云端怎么自动构建。

- 使用者看 [`README.md`](README.md)（怎么用、界面是什么）
- 改代码的深层参考看 [`DEVREADME.md`](DEVREADME.md)（架构、模块地图、契约、已知的坑）
  ——那份写得细，动手前值得通读一遍，尤其是第 3、5、6 节。

## 1. 环境（Windows 限定）

| 依赖 | 说明 |
|---|---|
| Python 3.9+ | 只用标准库，别引入 Flask / FastAPI 之类的 pip 包 |
| GCC | 编译要 `-g -O0`；实测 `C:\msys64\ucrt64\bin\gcc.exe` 可用 |
| GDB | **必须带 Python 支持**：`gdb --batch -ex "python print(1)"` 能打印 `1` 才算合格 |
| Edge | 前端截图/UI 回归用 `playwright-core`（在 `tools/node_modules`），走 `channel: "msedge"` |

只有 Windows：`memmap.py` 用 `VirtualQueryEx` 从进程外部查堆栈边界，Linux/macOS 直接跑不起来。

工具链配置 `config/toolchain.conf` 是唯一权威来源（扫描生成、读取分离）。首次开箱若配置缺失或路径失效，会自动重扫覆盖，所以整个目录拷到另一台机器也能跑。

```bash
python cdbg.py --toolchain                    # 看当前用哪个 gcc/gdb、配置在哪
python cdbg.py --scan-compiler                # 重扫 + TUI 选择
python cdbg.py --scan-compiler --select 2     # 非交互直选第 2 套
```

## 2. 本地跑起来

```bash
python cdbg.py examples/ch06_pointer.c              # 编译 + GDB + Web + 开浏览器
python cdbg.py prog.c --no-browser --port 8779      # 自己访问 http://127.0.0.1:8779/
python cdbg.py prog.c --headless                    # 终端输出快照，给 IDE/脚本调用
python cdbg.py prog.exe --keep-workdir              # 调试现成 exe，保留临时工作目录
python cdbg.py prog.c --no-instrument               # 关掉堆跟踪插桩
```

`cdbg.bat` / `start.bat` 是拖拽和"打开方式"入口，`scan_compiler.bat` 是工具链选择器，内部都是 `python cdbg.py ...`。

**注意**：这些 bat 不 `cd` 到脚本目录，**启动时的 cwd 不一定是项目根目录**。凡是涉及路径的代码都从 `__file__` 定位，别改成相对 cwd。

## 3. 改代码的几条硬规矩

这几条违反了不会立刻报错，但会埋下难查的回归，动手前先看一眼 `DEVREADME.md` 第 3、5、6 节：

1. **Snapshot 是前后端唯一契约。** 加字段要同时改三处：`snapshot.py` 的 dataclass → `collector.py` 填值 → 前端对应模块的 `render()` 消费。漏一步会静默丢数据（前端不报错，只是空着）。
2. **执行控制和采集是两条通道，别合并。** MI2 走 `gdb_session.py`，采集走注入的 `gdb_probe.py` + 约定快照文件；两边只靠"文件更新"这个信号同步。混用 GDB console 命令会让输出交错。
3. **新增按钮要改三处**：`index.html`（元素）、`app.js`（`bind()` 绑定 + `updateButtonStates()`）、`web/oplog.js` 的 `TOOLBAR_IDS`（否则操作日志不记这个按钮）。
4. **别在前端猜 `phase`**，它由后端在 SSE 里补；`snapshot` 可能是 `null`（首屏、清空后），这条路径必须保持可用。
5. **打包只能用 standalone，不能用 onefile。** onefile 把资源解压到 `%TEMP%\onefile_xxx`，而源码用 `__file__` 定位 `config/web/shim`，路径全乱、前端打不开。原因详见 `tools/build_standalone.py` 顶部注释。
6. **配置里存绝对路径**，判断"能不能用"必须落到 `os.path.isfile`，不能只看文件在不在。

## 4. 改完怎么验证

```bash
python tests/test_e2e.py                       # 端到端冒烟：编译 → GDB → 采集 → 单步
python tests/test_batch.py                     # 批量测 examples/
python tests/test_batch.py D:/cdesign          # 批量测任意目录
python tests/test_examples.py                  # 示例集
```

前端 UI 回归（在 `tools/` 下跑，需先起一个 `--no-browser --port 8779` 的实例）：

```bash
node shot.js http://127.0.0.1:8779/ shot.png btn-debug:2 btn-next:1
node ui_check.js http://127.0.0.1:8779/           # 断点开关全流程 + 三种行底色
node ui_clear_test.js http://127.0.0.1:8779/     # 清空界面（含刷新不复活）+ 长按停止
node ui_forcekill_test.js http://127.0.0.1:8779/ # 程序卡 scanf 时长按强制结束
```

排查编译问题用 `--keep-workdir` 保留临时目录；排查状态用 `--headless` 直接看终端快照。
调试 HTTP 时把 `web_server.py` 里 `_Handler.log_message` 的静默临时打开。

## 5. 云端构建与发布

工作流在 `.github/workflows/build.yml`，打包脚本在 `tools/build_standalone.py`。

**触发规则：**

| 方式 | 何时跑 |
|---|---|
| 手动 `workflow_dispatch` | 随时，去 Actions 页选 "Build cdbg (Windows standalone zip)" → Run workflow |
| 定时 `schedule` | 每天 UTC 17:00 = 北京时间次日凌晨 01:00 |
| 空跑保护 | 定时触发时对比 `last-built` tag 和当前 HEAD，没有新提交就跳过 |

**流程：** `check`（判断要不要构建）→ `build`（Windows runner + Python 3.14 + Nuitka + MSVC）→ 上传 artifact → 发布 `alpha` 预发布 release（changelog 留空）→ 打 `last-built` tag。

**产物：** `cdbg-windows-x64.zip`，解压即用。里面是 `cdbg.exe` + `cdbg/gdb_probe.py` + `web/` + `shim/` + `examples/` + 空 `config/` + `README.txt`。打包时会剔除本机 `toolchain.conf` 和 `log/`。

**本地复现云构建**（需要装了 Nuitka 的 `C:\Python314\python.exe`）：

```bash
python tools/build_standalone.py --output <pack下的独立目录> --msvc
```

本地构建产物按约定一律放到工作区的 `pack\` 下，**每次构建单独建一个文件夹**，不要污染源码目录；清理时分批手动删，别用批量删除（会触发风控）。

## 6. 提交与 PR

- 一个提交只做一件事，commit message 说清"改了什么、为什么"，别写 `update` 这种。
- 改了 `snapshot.py` 的字段，PR 描述里列出前后端同步改了哪几处。
- 动了 GDB 时序、断点、清空、强制结束这些逻辑，附上对应的 `ui_*.js` 回归结果。
- 不确定是不是回归的，先跑第 4 节全套测试再提。

## 7. 许可证与致谢

本项目用 **MIT License**（见 [`LICENSE`](LICENSE)）。提交 PR 即表示你同意自己的贡献也按 MIT 授权。

交互设计参考了 **Red Panda Dev C++（小熊猫 Dev-C++，royqh1979/RedPanda-CPP）**：快捷键语义、GDB 启动方式、工具链字段命名等，目的是让 C 语言初学者从 Dev-C++ 切过来不用重新学。上游项目以 **GPL v3** 发布（`Dev-CPP-master.zip` 内 `LICENSE` 为证）；本项目是独立的 Python/HTML 实现，**未包含其源码**，只参考了行为设计——所以这里用 MIT 授权，并在致谢里注明来源。

`Dev-CPP-master.zip` 那份源码包仅作参考，已进 `.gitignore`，不要提交进仓库。
