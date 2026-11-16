#!/bin/bash
# Build a double-clickable macOS .app for the local Telegram batch downloader.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
APP_NAME="TelegramBatchDownloader"
APP="dist/$APP_NAME.app"

if [ ! -x "$PY" ]; then
  echo "找不到可用的 Python：$PY" >&2
  echo "先创建虚拟环境：python3 -m venv .venv && .venv/bin/python -m pip install -r requirements-runtime.lock.txt" >&2
  exit 1
fi

echo "== 安装打包依赖 =="
if ! "$PY" -m pip install -q -r requirements-build.txt; then
  echo "官方源不可用，改用清华镜像重试"
  "$PY" -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-build.txt
fi

echo "== 生成 .icns 图标 =="
generate_icon() {
  if [ ! -f assets/icon.svg ]; then return; fi
  command -v qlmanage >/dev/null 2>&1 || { echo "跳过图标：缺少 qlmanage"; return; }
  tmp="$(mktemp -d)"
  if ! qlmanage -t -s 1024 -o "$tmp" assets/icon.svg >/dev/null 2>&1; then
    echo "跳过图标：SVG 渲染失败"; rm -rf "$tmp"; return
  fi
  png="$tmp/icon.svg.png"
  if [ ! -f "$png" ]; then echo "跳过图标：未生成 PNG"; rm -rf "$tmp"; return; fi
  iconset="$tmp/icon.iconset"; mkdir -p "$iconset"
  sips -z 16 16 "$png" --out "$iconset/icon_16x16.png" >/dev/null
  sips -z 32 32 "$png" --out "$iconset/icon_16x16@2x.png" >/dev/null
  sips -z 32 32 "$png" --out "$iconset/icon_32x32.png" >/dev/null
  sips -z 64 64 "$png" --out "$iconset/icon_32x32@2x.png" >/dev/null
  sips -z 128 128 "$png" --out "$iconset/icon_128x128.png" >/dev/null
  sips -z 256 256 "$png" --out "$iconset/icon_128x128@2x.png" >/dev/null
  sips -z 256 256 "$png" --out "$iconset/icon_256x256.png" >/dev/null
  sips -z 512 512 "$png" --out "$iconset/icon_256x256@2x.png" >/dev/null
  sips -z 512 512 "$png" --out "$iconset/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$png" --out "$iconset/icon_512x512@2x.png" >/dev/null
  iconutil -c icns "$iconset" -o assets/icon.icns
  rm -rf "$tmp"
}
generate_icon

echo "== 使用 PyInstaller 打包 =="
ICON_ARG=()
if [ -f assets/icon.icns ]; then ICON_ARG=(--icon assets/icon.icns); fi
"$PY" -m PyInstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "com.sidneypearson.telegrambatchdownloader" \
  "${ICON_ARG[@]}" \
  --add-data "README.md:." \
  --add-data "THIRD-PARTY-NOTICES.md:." \
  --add-data "licenses:licenses" \
  app.py

echo "== 调整 Info.plist 显示名 =="
PLIST="$APP/Contents/Info.plist"
if [ -f "$PLIST" ]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName Telegram 批量下载器" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string Telegram 批量下载器" "$PLIST"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName Telegram 批量下载器" "$PLIST" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :NSHighResolutionCapable true" "$PLIST"
fi

echo "== 本机临时签名 =="
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "签名跳过（不影响本机运行）"

echo
echo "完成：$ROOT/$APP"
echo "数据目录：$HOME/Library/Application Support/TelegramBatchDownloader"
echo "如需沿用源码版的队列，先完全退出程序，然后执行："
echo "  mkdir -p \"$HOME/Library/Application Support/TelegramBatchDownloader\" && cp -R state \"$HOME/Library/Application Support/TelegramBatchDownloader/\""
