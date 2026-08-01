#!/usr/bin/env bash
# 一键安装：自动克隆仓库并安装。用法：
#   bash <(curl -fsSL https://raw.githubusercontent.com/zhangchedan000/oci-panel/main/bootstrap.sh)
set -euo pipefail
REPO="https://github.com/zhangchedan000/oci-panel.git"
DIR="${OCI_PANEL_DIR:-$HOME/oci-panel}"

echo "==> 检查 git"
command -v git >/dev/null 2>&1 || { sudo apt-get update -y && sudo apt-get install -y git; }

if [ -d "$DIR/.git" ]; then
  echo "==> 已存在，拉取最新代码：$DIR"
  git -C "$DIR" pull --ff-only || true
else
  echo "==> 克隆到 $DIR"
  git clone "$REPO" "$DIR"
fi

echo "==> 运行安装"
bash "$DIR/install.sh"
