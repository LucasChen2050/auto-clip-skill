#!/bin/bash
# 准备打包用的“便携 Python”：python-build-standalone + 依赖（各平台各一份），以及语音识别模型。
# 用法：bash scripts/prepare_runtime.sh            # 默认 4 个平台
#       TARGETS="aarch64-apple-darwin" bash scripts/prepare_runtime.sh
# 产物：build/rt/<平台>/python   build/models/
set -e
cd "$(dirname "$0")/.."
PY=3.12.13
PBS_TAG="${PBS_TAG:-20260414}"   # https://github.com/astral-sh/python-build-standalone/releases 里含 3.12.13 的发布日期，按实际修改
TARGETS="${TARGETS:-aarch64-apple-darwin x86_64-apple-darwin x86_64-pc-windows-msvc x86_64-unknown-linux-gnu}"
mkdir -p build/rt build/models

plat() {  # pip 的平台标签
  case $1 in
    aarch64-apple-darwin) echo "--platform macosx_11_0_arm64" ;;
    x86_64-apple-darwin) echo "--platform macosx_10_15_x86_64 --platform macosx_11_0_x86_64" ;;
    x86_64-pc-windows-msvc) echo "--platform win_amd64" ;;
    x86_64-unknown-linux-gnu) echo "--platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64" ;;
  esac
}

for T in $TARGETS; do
  D=build/rt/$T
  if [ ! -d "$D/python" ]; then
    mkdir -p "$D"
    url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY}+${PBS_TAG}-${T}-install_only.tar.gz"
    echo "下载 $url"
    curl -L --fail "$url" | tar -xz -C "$D"
  fi
  if [ "$T" = "x86_64-pc-windows-msvc" ]; then SP="$D/python/Lib/site-packages"; else SP="$D/python/lib/python3.12/site-packages"; fi
  python3 -m pip install --target "$SP" $(plat "$T") --only-binary=:all: --python-version 3.12 \
    --implementation cp --upgrade -r requirements.txt
done

# 语音识别模型（SenseVoice int8）+ 静音检测（Silero VAD）
if [ ! -f build/models/silero_vad.onnx ]; then
  curl -L --fail -o build/models/silero_vad.onnx \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
fi
M=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
if [ ! -d "build/models/$M" ]; then
  curl -L --fail https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$M.tar.bz2 | tar -xj -C build/models
fi
echo "完成。接着运行：bash scripts/build_packages.sh"
