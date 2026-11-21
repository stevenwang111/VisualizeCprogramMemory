#!/usr/bin/env bash
# cdbg 启动脚本（Linux / WSL2）。
# 用法：./start.sh prog.c [其它 cdbg.py 参数]
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：找不到 python3。请安装 Python 3.9+。" >&2
  exit 1
fi

missing=0
for tool in gcc gdb; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "错误：找不到 $tool。" >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "请安装：sudo apt install gcc gdb" >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  echo "用法：$0 <源文件.c 或可执行文件> [--no-browser ...]"
  exit 2
fi

exec python3 cdbg.py "$@"
