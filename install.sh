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
  # minimal config: no accounts, no password — both are set later in the web UI
  cat > "$DIR/accounts.yaml" << 'YML'
panel:
  username: "admin"
  password_hash: ""
accounts: []
YML
  echo "==> 已生成初始配置（无需手填账号/密码，稍后全在网页里完成）。"
fi


echo "==> 加固密钥权限"
mkdir -p "$DIR/keys"
chmod 700 "$DIR/keys" || true
chmod 600 "$DIR"/keys/*.pem 2>/dev/null || true
chmod 600 "$DIR/accounts.yaml" "$DIR/identity.json" "$DIR/settings.json" "$DIR/setup_token.txt" 2>/dev/null || true

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
sleep 2
if [ -f "$DIR/setup_token.txt" ]; then
  echo ""
  echo "  ★ 首次设置：打开上面地址，输入下面这个令牌来设置登录用户名/密码"
  echo "     设置令牌： $(cat "$DIR/setup_token.txt")"
  echo "     (设置完成后此令牌自动失效)"
  echo "============================================================"
fi

if [ "$HOST" = "0.0.0.0" ]; then
  echo ""
  echo "⚠ 面板已绑定到公网 (0.0.0.0:$PORT)，明文 HTTP，握有你的云密钥。"
  echo "  强烈建议：1) 面板密码设强一点  2) 防火墙只放行你自己的 IP"
  echo "            3) 尽快用 NPM/Caddy 加 HTTPS 反代后，把 HOST 改回 127.0.0.1"
  echo "  放行端口示例(仅放行你的IP)： sudo ufw allow from 你的IP to any port $PORT"
fi
