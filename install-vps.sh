#!/usr/bin/env bash
set -euo pipefail

# Impact Motion static-site installer for Ubuntu (including ARM64/AArch64 VPS).
# Run from the extracted project directory: sudo bash install-vps.sh

SITE_NAME="impact-motion"
WEB_ROOT="/var/www/${SITE_NAME}"
NGINX_SITE="/etc/nginx/sites-available/${SITE_NAME}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${SITE_NAME}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo bash install-vps.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer is intended for Ubuntu/Debian systems using apt."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${SCRIPT_DIR}/index.html" || ! -d "${SCRIPT_DIR}/templates" ]]; then
  echo "Run this script from the extracted Impact Motion project directory."
  exit 1
fi

echo "[1/5] Installing Nginx..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx

echo "[2/5] Installing website files..."
install -d -o www-data -g www-data -m 0755 "${WEB_ROOT}"
cp -a "${SCRIPT_DIR}/." "${WEB_ROOT}/"
chown -R www-data:www-data "${WEB_ROOT}"
find "${WEB_ROOT}" -type d -exec chmod 755 {} \;
find "${WEB_ROOT}" -type f -exec chmod 644 {} \;

echo "[3/5] Configuring Nginx..."
cat > "${NGINX_SITE}" <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    root /var/www/impact-motion;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }

    location ~* \.(xml|js|css|html)$ {
        try_files $uri =404;
        add_header Cache-Control "no-cache";
    }

    location ~ /\. {
        deny all;
    }

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
}
NGINX

# Disable the default site so the Impact Motion site owns port 80.
rm -f /etc/nginx/sites-enabled/default
ln -sfn "${NGINX_SITE}" "${NGINX_ENABLED}"

echo "[4/5] Checking Nginx configuration..."
nginx -t

echo "[5/5] Enabling and starting Nginx..."
systemctl enable nginx
systemctl restart nginx

PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Impact Motion has been installed."
echo "Open: http://${PUBLIC_IP:-YOUR_SERVER_IP}/"
echo
echo "If the page cannot be reached, allow inbound TCP port 80 in your VPS cloud firewall."
echo "For HTTPS and a domain name, configure DNS and then install a TLS certificate."
