"""打开浏览器的兼容封装。

Windows 上 `os.startfile` 最省事，但在某些终端环境下会静默失败，
所以这里带一次兜底检查。
"""

from __future__ import annotations

import os
import sys
import webbrowser


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
