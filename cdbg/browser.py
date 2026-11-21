"""打开浏览器的兼容封装。

- Windows：`os.startfile` 最省事，失败再退回常见浏览器路径；
- 普通 Linux：优先 `webbrowser.open`（内部走 xdg-open 等）；
- WSL：优先 `wslview`（wslu 提供），没有就明确返回 False，
  由调用方提示用户用 `--no-browser` 加 Windows 浏览器访问 127.0.0.1。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser


def _wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))


def open_browser(url: str, timeout: float = 3.0) -> bool:
    """尝试打开浏览器，返回是否成功。

    失败不是致命错误 —— 用户可以手动访问打印出来的地址。
    """
    if not url:
        return False

    if sys.platform == "win32":
        try:
            os.startfile(url)  # noqa: S606
            return True
        except Exception:
            pass

        for opener in (webbrowser.open,):
            try:
                if opener(url):
                    return True
            except Exception:
                continue

        # 最后的兜底：常见的浏览器可执行文件
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
        ]
        for cand in candidates:
            if os.path.isfile(cand):
                try:
                    os.startfile(cand, url)  # noqa: S606
                    return True
                except Exception:
                    continue
        return False

    # ---- POSIX / WSL ----
    if _wsl():
        wslview = shutil.which("wslview")
        if wslview:
            try:
                subprocess.Popen(
                    [wslview, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except OSError:
                pass
        # WSL 里通常没有 xdg-open；交给 webbrowser 试一次，失败就明确返回
        try:
            if webbrowser.open(url):
                return True
        except Exception:
            pass
        return False

    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    return False
