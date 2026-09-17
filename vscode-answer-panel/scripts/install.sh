#!/usr/bin/env bash
# 把 .vsix 侧载进本机所有能认出来的 VSCode 系编辑器。
#
#   ./scripts/install.sh              # 自动探测并安装到全部
#   ./scripts/install.sh qoder        # 只装 Qoder IDE
#   ./scripts/install.sh cursor trae  # 装指定的几个
#
# 注意：编辑器在运行时，装完需要重载窗口（Cmd+Shift+P → Reload Window）才会生效。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VSIX="$(ls -t ./*.vsix 2>/dev/null | head -1 || true)"
if [ -z "$VSIX" ]; then
  echo "找不到 .vsix，先跑 ./scripts/build.sh" >&2
  exit 1
fi
echo "==> 使用 $VSIX"

# 名称 -> 可执行文件候选路径（macOS .app 内部 bin，以及 PATH 上的命令）
candidates() {
  case "$1" in
    qoder)   echo "/Applications/Qoder IDE.app/Contents/Resources/app/bin/code|/Applications/Qoder IDE.app/Contents/Resources/app/bin/qoder|qoder" ;;
    vscode)  echo "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code|code" ;;
    cursor)  echo "/Applications/Cursor.app/Contents/Resources/app/bin/cursor|cursor" ;;
    trae)    echo "/Applications/Trae.app/Contents/Resources/app/bin/trae|/Applications/Trae CN.app/Contents/Resources/app/bin/trae|trae" ;;
    windsurf) echo "/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf|windsurf" ;;
    codium)  echo "/Applications/VSCodium.app/Contents/Resources/app/bin/codium|codium" ;;
    *)       echo "$1" ;;
  esac
}

ALL=(qoder vscode cursor trae windsurf codium)
[ "$#" -gt 0 ] && TARGETS=("$@") || TARGETS=("${ALL[@]}")

ok=0
for name in "${TARGETS[@]}"; do
  bin=""
  IFS='|' read -ra paths <<<"$(candidates "$name")"
  for p in "${paths[@]}"; do
    if [ -x "$p" ]; then bin="$p"; break; fi
    if command -v "$p" >/dev/null 2>&1; then bin="$(command -v "$p")"; break; fi
  done
  if [ -z "$bin" ]; then
    echo "  --   $name：没找到，跳过"
    continue
  fi
  echo "  -->   $name（$bin）"
  if "$bin" --install-extension "$VSIX" --force >/dev/null 2>&1; then
    echo "        ✅ 已安装"
    ok=$((ok + 1))
  else
    echo "        ❌ 安装失败，试着手动执行："
    echo "           \"$bin\" --install-extension \"$HERE/${VSIX#./}\""
  fi
done

echo
echo "装好了 $ok 个。请在这些编辑器里执行一次 Reload Window。"
[ "$ok" -eq 0 ] && exit 1 || exit 0
