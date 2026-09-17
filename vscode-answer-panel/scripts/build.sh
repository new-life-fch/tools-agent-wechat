#!/usr/bin/env bash
# 打包成 .vsix。产物落在本目录。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

export npm_config_cache="${npm_config_cache:-$HERE/.npm-cache}"

if [ ! -d node_modules ]; then
  echo "==> 首次运行，安装打包工具（@vscode/vsce）"
  npm install --no-audit --no-fund
fi

echo "==> 离线测试"
node test/offline.test.js

echo "==> 打包"
npx --no-install vsce package --no-dependencies --allow-missing-repository "$@"

ls -lh ./*.vsix
