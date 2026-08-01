#!/usr/bin/env bash
# 一键卸载：停止并移除服务；可选连目录(含密钥)一起删。用法：
#   bash <(curl -fsSL https://raw.githubusercontent.com/zhangchedan000/oci-panel/main/uninstall.sh)
# 非交互彻底删除： PURGE=1 bash <(curl -fsSL .../uninstall.sh)
set -euo pipefail
DIR="${OCI_PANEL_DIR:-$HOME/oci-panel}"
SVC="oci-panel"

echo "==> 停止并移除 systemd 服务"
sudo systemctl disable --now "$SVC" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$SVC.service"
sudo systemctl daemon-reload 2>/dev/null || true

PURGE="${PURGE:-}"
if [ -z "$PURGE" ]; then
  if [ -e /dev/tty ]; then
    printf "是否连同目录一起删除？会删掉 %s 里的密钥和 accounts.yaml [y/N]: " "$DIR" > /dev/tty
    read -r ans < /dev/tty || ans=""
    case "$ans" in [yY]*) PURGE=1;; *) PURGE=0;; esac
  else
    PURGE=0
  fi
fi

if [ "$PURGE" = "1" ]; then
  rm -rf "$DIR"
  echo "==> 已删除 $DIR（含密钥）。卸载完成。"
else
  echo "==> 服务已卸载。代码和密钥保留在 $DIR"
  echo "    如需彻底删除： rm -rf $DIR"
fi
