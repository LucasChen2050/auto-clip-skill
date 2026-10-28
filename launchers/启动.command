#!/bin/bash
# 全自动化剪辑 Skill · Mac 版（双击运行，无需安装任何东西）
cd "$(dirname "$0")" || exit 1

PKG_ARCH="__ARCH__"
if [ "$PKG_ARCH" = "arm64" ] && [ "$(uname -m)" != "arm64" ]; then
  echo ""
  echo "这台 Mac 是 Intel 芯片，请改用 Intel 版。"
  read -r -p "按回车键退出" _
  exit 1
fi

if [ ! -f runtime/.ready ]; then
  echo "首次运行，正在做准备（几秒钟）…"
  # 解除从网上下载带来的“隔离”标记，否则 macOS 会逐个拦截内置程序
  xattr -dr com.apple.quarantine . 2>/dev/null
  chmod +x runtime/bin/* 2>/dev/null
  find runtime -name "ffmpeg-*" -exec chmod +x {} \; 2>/dev/null
  touch runtime/.ready
fi

./runtime/bin/python3 run.py
