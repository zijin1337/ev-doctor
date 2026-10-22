#!/usr/bin/env bash
# 电诊 EV-Doctor 一键部署（Ubuntu / Debian / CentOS / Rocky 都行）
#
# 用法（在服务器上，root 或有 sudo 的用户）：
#   git clone https://github.com/<你>/ev-doctor.git /opt/ev-doctor
#   cd /opt/ev-doctor && sudo bash deploy/deploy.sh
#
# 可选环境变量：
#   DOMAIN=ev.example.com     有域名就填，脚本会配 nginx 反代并尝试签 HTTPS 证书
#   PORT=8000                 后端端口（只监听本机，对外走 nginx 80/443）
#   ADMIN_TOKEN=xxxx          远程改"设置"用的口令；不填会随机生成并打印出来
#   NO_NGINX=1                不装 nginx，直接用 0.0.0.0:PORT 对外（临时演示可以，不推荐）
#
# 再次运行 = 更新：会 git pull、重启服务，数据库、config.json、端口和管理口令都沿用上次的。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 再次运行时沿用上次部署的端口（比如 8000 被别的服务占了、当初指定过 PORT=8010），避免更新时被改回 8000
PORT="${PORT:-$(sed -n 's/^EVDOC_PORT=//p' /etc/default/ev-doctor 2>/dev/null || true)}"
PORT="${PORT:-8000}"
DOMAIN="${DOMAIN:-}"
SERVICE=ev-doctor
RUN_USER=evdoctor

say()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "请用 root 运行：sudo bash deploy/deploy.sh"

# ---------------------------------------------------------------- 1. Python
say "检查 Python"
pick_py() {  # 找一个 >= 3.8 的解释器
  for c in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null && { echo "$p"; return 0; }
  done
  return 1
}
PY=$(pick_py || true)
if [ -z "$PY" ]; then
  say "系统 Python 低于 3.8，安装 python3.11（不影响系统自带的）"
  if command -v dnf >/dev/null; then dnf install -y -q python3.11 >/dev/null
  elif command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 >/dev/null
  elif command -v yum >/dev/null; then yum install -y -q python3 >/dev/null
  else die "找不到 python3，请先安装"; fi
  PY=$(pick_py || true)
  [ -n "$PY" ] || die "需要 Python 3.8 以上"
fi
say "Python: $($PY --version)"

# ---------------------------------------------------------------- 2. 用户、目录、权限
say "准备运行用户和目录"
id -u $RUN_USER >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin $RUN_USER
mkdir -p "$APP_DIR/data/backups"
[ -f "$APP_DIR/config.json" ] || printf '{\n  "api_base": "",\n  "api_key": "",\n  "model": "",\n  "vision_model": ""\n}\n' > "$APP_DIR/config.json"
# 代码目录：root 拥有、运行用户可读（从 Windows / 压缩包解出来的文件权限经常不对）
chown -R root:root "$APP_DIR"
find "$APP_DIR" -type d -exec chmod 755 {} +
find "$APP_DIR" -type f -exec chmod 644 {} +
chmod 755 "$APP_DIR/deploy/deploy.sh"
# 数据目录和配置：运行用户拥有
chown -R $RUN_USER:$RUN_USER "$APP_DIR/data" "$APP_DIR/config.json"
chmod 600 "$APP_DIR/config.json"            # 里面可能有 key

# ---------------------------------------------------------------- 3. 管理口令
ENV_FILE=/etc/default/$SERVICE
if [ -f "$ENV_FILE" ] && grep -q '^EVDOC_ADMIN_TOKEN=' "$ENV_FILE"; then
  ADMIN_TOKEN="${ADMIN_TOKEN:-$(sed -n 's/^EVDOC_ADMIN_TOKEN=//p' "$ENV_FILE")}"
fi
ADMIN_TOKEN="${ADMIN_TOKEN:-$($PY -c 'import secrets;print(secrets.token_urlsafe(18))')}"
BIND_HOST=127.0.0.1; [ "${NO_NGINX:-}" = 1 ] && BIND_HOST=0.0.0.0
cat > "$ENV_FILE" <<EOF
EVDOC_HOST=$BIND_HOST
EVDOC_PORT=$PORT
EVDOC_ADMIN_TOKEN=$ADMIN_TOKEN
EVDOC_RATE=120
EVDOC_BACKUP_HOUR=3
EOF
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------- 4. systemd 服务
say "写 systemd 服务"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=EV-Doctor 新能源汽车故障诊断
After=network.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY $APP_DIR/server.py
Restart=always
RestartSec=3
# 只给它需要的权限
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data $APP_DIR/config.json
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable $SERVICE >/dev/null
systemctl restart $SERVICE
sleep 1.5
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { journalctl -u $SERVICE -n 30 --no-pager; die "服务没起来，日志见上"; }
say "后端已运行：127.0.0.1:$PORT"

# ---------------------------------------------------------------- 5. nginx
if [ "${NO_NGINX:-}" != 1 ]; then
  say "配置 nginx"
  if ! command -v nginx >/dev/null; then
    if command -v apt-get >/dev/null; then apt-get install -y -qq nginx >/dev/null
    elif command -v dnf >/dev/null; then dnf install -y -q nginx
    else yum install -y -q nginx; fi
  fi
  SERVER_NAME="${DOMAIN:-_}"
  CONF=/etc/nginx/conf.d/$SERVICE.conf
  [ -d /etc/nginx/sites-enabled ] && { CONF=/etc/nginx/sites-available/$SERVICE; ln -sf "$CONF" /etc/nginx/sites-enabled/$SERVICE; rm -f /etc/nginx/sites-enabled/default; }
  cat > "$CONF" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAME;
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;          # /api/explain 是流式输出
        proxy_read_timeout 120s;
    }
}
EOF
  nginx -t >/dev/null && systemctl enable nginx >/dev/null && systemctl restart nginx
  say "nginx 已配置（80 端口）"

  if [ -n "$DOMAIN" ]; then
    say "尝试签发 HTTPS 证书（$DOMAIN）"
    if ! command -v certbot >/dev/null; then
      if command -v apt-get >/dev/null; then apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1 || true
      else (dnf install -y -q certbot python3-certbot-nginx || yum install -y -q certbot python3-certbot-nginx) >/dev/null 2>&1 || true; fi
    fi
    if command -v certbot >/dev/null; then
      certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect \
        && say "HTTPS 已开启" || say "证书签发失败（域名是否已解析到本机？），先用 http 访问"
    else
      say "certbot 装不上，先用 http 访问"
    fi
  fi
fi

# ---------------------------------------------------------------- 6. 防火墙
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  [ "${NO_NGINX:-}" = 1 ] && ufw allow "$PORT"/tcp >/dev/null || { ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; }
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  if [ "${NO_NGINX:-}" = 1 ]; then firewall-cmd -q --permanent --add-port="$PORT"/tcp; else firewall-cmd -q --permanent --add-service=http; firewall-cmd -q --permanent --add-service=https; fi
  firewall-cmd -q --reload
fi

# ---------------------------------------------------------------- 7. 完成
IP=$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
echo
say "部署完成"
if [ "${NO_NGINX:-}" = 1 ]; then URL="http://$IP:$PORT"
elif [ -n "$DOMAIN" ]; then URL="https://$DOMAIN（若证书失败则 http://$DOMAIN）"
else URL="http://$IP"; fi
cat <<EOF

  访问地址：$URL
  管理口令：$ADMIN_TOKEN
      （在网页右上角"设置接口"里改大模型配置时，按提示填这个管理口令即可，浏览器会记住；
        或者直接 ssh 上来改 $APP_DIR/config.json）

  常用命令：
    systemctl status $SERVICE        看状态
    journalctl -u $SERVICE -f        看实时日志
    tail -f $APP_DIR/data/server.log 看访问日志
    systemctl restart $SERVICE       重启
    cd $APP_DIR && git pull && systemctl restart $SERVICE   更新代码
    ls $APP_DIR/data/backups/        每天 3 点自动备份的数据库

  云服务器记得在控制台的「安全组」放行 80/443（或 $PORT）端口。
EOF
