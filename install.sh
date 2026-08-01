#!/usr/bin/env bash
# OCI 面板一键安装 (Debian/Ubuntu)。在项目目录下运行： bash install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9000}"

echo "==> 安装依赖"
sudo apt-get update -y && sudo apt-get install -y python3-venv python3-pip

echo "==> 创建虚拟环境"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

if [ ! -f "$DIR/accounts.yaml" ]; then
  cp "$DIR/accounts.example.yaml" "$DIR/accounts.yaml"
  # --- set panel login user/password interactively (hashed, not plaintext) ---
  if [ -e /dev/tty ]; then
    printf "设置面板登录用户名 [admin]: " > /dev/tty; read -r PU < /dev/tty || PU=""
    PU="${PU:-admin}"
    while :; do
      printf "设置面板登录密码: " > /dev/tty; read -rs PP < /dev/tty; echo > /dev/tty
      printf "再输一次确认: " > /dev/tty; read -rs PP2 < /dev/tty; echo > /dev/tty
      [ -n "$PP" ] && [ "$PP" = "$PP2" ] && break
      echo "  两次不一致或为空，重来。" > /dev/tty
    done
    HASH="$("$DIR/.venv/bin/python" "$DIR/auth.py" "$PP")"
    python3 - "$DIR/accounts.yaml" "$PU" "$HASH" << 'PYIN'
import sys, re
path, user, h = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path, encoding="utf-8").read()
s = re.sub(r'username:\s*".*?"', f'username: "{user}"', s, count=1)
s = re.sub(r'password_hash:\s*".*?"', f'password_hash: "{h}"', s, count=1)
open(path, "w", encoding="utf-8").write(s)
PYIN
    echo "==> 登录用户名/密码已设置（密码以哈希存储）。" > /dev/tty
  else
    echo "==> 无交互终端，请手动编辑 $DIR/accounts.yaml 的 panel 段设置密码。"
  fi
  echo "==> 已生成 accounts.yaml。接着编辑它填 [每个账号的 API Key + 出口 IP]，"
  echo "    然后重跑一键安装命令（或 bash $DIR/install.sh）完成安装。"
  exit 0
fi


echo "==> 加固密钥权限"
mkdir -p "$DIR/keys"
chmod 700 "$DIR/keys" || true
chmod 600 "$DIR"/keys/*.pem 2>/dev/null || true
chmod 600 "$DIR/accounts.yaml" "$DIR/identity.json" "$DIR/settings.json" 2>/dev/null || true

echo "==> 写入 systemd 服务"
sudo tee /etc/systemd/system/oci-panel.service >/dev/null << UNIT
[Unit]
Description=OCI Panel
After=network.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/uvicorn app:app --host $HOST --port $PORT
Restart=on-failure
User=$USER

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now oci-panel
echo "==> 完成。服务监听 $HOST:$PORT"
echo "   下一步：用 Nginx/Caddy 反代到该端口并配 HTTPS，再从浏览器访问。"
echo "   查看日志： journalctl -u oci-panel -f"
IP="$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "============================================================"
if [ "$HOST" = "0.0.0.0" ]; then
  echo "  控制台地址：  http://${IP:-你的公网IP}:$PORT"
  echo "  (记得放行端口：云安全组 + 本机 sudo ufw allow $PORT)"
else
  echo "  控制台地址：  http://127.0.0.1:$PORT  (仅本机；用 SSH 隧道或反代访问)"
fi
echo "  登录用户名：  $(grep -oP 'username:\s*"\K[^"]+' "$DIR/accounts.yaml" 2>/dev/null || echo admin)"
echo "============================================================"

if [ "$HOST" = "0.0.0.0" ]; then
  echo ""
  echo "⚠ 面板已绑定到公网 (0.0.0.0:$PORT)，明文 HTTP，握有你的云密钥。"
  echo "  强烈建议：1) 面板密码设强一点  2) 防火墙只放行你自己的 IP"
  echo "            3) 尽快用 NPM/Caddy 加 HTTPS 反代后，把 HOST 改回 127.0.0.1"
  echo "  放行端口示例(仅放行你的IP)： sudo ufw allow from 你的IP to any port $PORT"
fi
