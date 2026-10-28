#!/bin/bash
# 组装各平台的离线安装包（解压即用）：便携 Python + 依赖 + 模型 + 工具本体
# 先运行 scripts/prepare_runtime.sh 准备 build/rt 和 build/models。
# 用法：bash scripts/build_packages.sh            # 全部平台
#       TARGETS="mac-arm win" bash scripts/build_packages.sh
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
RT="$SRC/build/rt"
MODELS="$SRC/build/models"
OUT="$SRC/dist"
mkdir -p "$OUT" && rm -rf "$OUT"/stage_*
EDITION=lingjun
DIR="口播快剪"; [ "$EDITION" = "lingjun" ] && DIR="全自动化剪辑Skill"

trim_runtime() {   # $1 = python 目录
  local P="$1"
  rm -rf "$P/include" "$P/share" "$P/tcl" "$P/libs"
  find "$P" -depth -type d \( -name "__pycache__" -o -name "idlelib" -o -name "tkinter" -o -name "turtledemo" \
       -o -name "ensurepip" -o -name "test" -o -name "tests" -o -name "include" -o -name "pip" -o -name "pip-*.dist-info" \) \
       -prune -exec rm -rf {} + 2>/dev/null || true
  find "$P" -name "*.lib" -delete
  find "$P" \( -name "tcl8*" -o -name "tk8*" -o -name "itcl*" -o -name "thread2*" -o -name "_tkinter*" -o -name "tcl86t.dll" -o -name "tk86t.dll" \) -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf "$P"/lib/python3.12/site-packages/bin "$P"/Lib/site-packages/Scripts "$P"/Lib/site-packages/bin
}

build() {  # $1 平台目录  $2 包名  $3 类型(mac/win/linux)
  local T="$1" NAME="$2" KIND="$3"
  local D="$OUT/stage_$NAME/$DIR"
  rm -f "$OUT/$NAME.zip"
  mkdir -p "$D"
  cp -r "$SRC/app" "$SRC/fonts" "$SRC/templates" "$SRC/vendor" "$SRC/sfx" "$SRC/run.py" "$D/"
  rm -f "$D"/templates/*/preview.jpg.cover.jpg
  cp "$SRC/edition.json" "$SRC/使用说明.md" "$D/"
  mkdir -p "$D/music/user"
  mkdir -p "$D/workspace" "$D/output" "$D/models/sense-voice"
  touch "$D/workspace/.keep" "$D/output/.keep"
  cp "$MODELS"/sherpa-onnx-sense-voice*/model.int8.onnx "$MODELS"/sherpa-onnx-sense-voice*/tokens.txt "$D/models/sense-voice/"
  cp "$MODELS/silero_vad.onnx" "$D/models/"
  cp -a "$RT/$T/python" "$D/runtime"
  trim_runtime "$D/runtime"
  find "$D" -name "__pycache__" -prune -exec rm -rf {} +
  case $KIND in
    mac)   cp "$SRC/launchers/启动.command" "$D/"; chmod +x "$D/启动.command"
           sed -i "s/__ARCH__/$4/; s/「口播快剪-Mac-Intel」这个版本/ Intel 版/; s/口播快剪 · Mac 离线版/$DIR · Mac 离线版/" "$D/启动.command" ;;
    win)   sed 's/$/\r/' "$SRC/launchers/启动.bat" > "$D/启动.bat" ;;
    linux) printf '#!/bin/bash\ncd "$(dirname "$0")"\n./runtime/bin/python3 run.py "$@"\n' > "$D/start.sh"; chmod +x "$D/start.sh" ;;
  esac
  if [ "$KIND" = "win" ]; then   # Windows：写入 UTF-8 文件名标记，系统自带解压不会乱码
    python3 -c "
import zipfile,os,sys
src,out=sys.argv[1],sys.argv[2]
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for r,ds,fs in os.walk(src):
        for f in fs:
            full=os.path.join(r,f); z.write(full,os.path.relpath(full,src))
" "$OUT/stage_$NAME" "$OUT/$NAME.zip"
  else
    (cd "$OUT/stage_$NAME" && zip -qry -X "$OUT/$NAME.zip" "$DIR")
  fi
  echo "$NAME: $(du -sh "$D" | cut -f1) 解压后, zip $(du -h "$OUT/$NAME.zip" | cut -f1)"
}

for t in ${TARGETS:-mac-arm mac-intel win}; do case $t in
    linux) build x86_64-unknown-linux-gnu 全自动化剪辑Skill-Linux测试 linux ;;
    mac-arm) build aarch64-apple-darwin 全自动化剪辑Skill-Mac-M芯片 mac arm64 ;;
    mac-intel) build x86_64-apple-darwin 全自动化剪辑Skill-Mac-Intel mac x86_64 ;;
    win) build x86_64-pc-windows-msvc 全自动化剪辑Skill-Windows win ;;
  esac; done
