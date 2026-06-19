#!/usr/bin/env bash
# ============================================================================
# Finanzas Familia — instalador para un VPS Debian 12 (1-2 GB RAM).
# Instala dependencias, prepara la app + el puente WhatsApp y los deja
# corriendo con PM2 detrás de HTTPS (Caddy). Idempotente: se puede repetir.
#
# Uso (como usuario con sudo, dentro de la carpeta del proyecto):
#   bash scripts/deploy-debian.sh tu-dominio.cl
# ============================================================================
set -euo pipefail

DOMINIO="${1:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Finanzas Familia · deploy en $(hostname) — proyecto en $ROOT"

# 1) Swap de 2 GB (en VPS de 1 GB) ----------------------------------------
if ! swapon --show | grep -q /swapfile; then
  echo "==> Creando swap de 2 GB"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# 2) Dependencias del sistema ------------------------------------------------
echo "==> Instalando dependencias (python, node, caddy)"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl \
  debian-keyring debian-archive-keyring apt-transport-https

if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
  sudo apt-get install -y nodejs
fi
command -v pm2 >/dev/null || sudo npm install -g pm2

if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update -y && sudo apt-get install -y caddy
fi

# 3) App Python --------------------------------------------------------------
echo "==> Entorno Python"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  SECRET="$(openssl rand -hex 32)"
  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=${SECRET}/" .env
  sed -i "s/^ENV=.*/ENV=production/" .env
  echo "==> .env creado. EDITA: DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, GOOGLE_CLIENT_ID/SECRET, SUPER_ADMIN_EMAIL, APP_URL"
fi

# 4) Arrancar con PM2 --------------------------------------------------------
echo "==> Arrancando con PM2"
pm2 start ecosystem.config.js || pm2 restart ecosystem.config.js
pm2 save
sudo env PATH=$PATH pm2 startup systemd -u "$USER" --hp "$HOME" | tail -1 | bash || true

# 5) HTTPS con Caddy ---------------------------------------------------------
if [ -n "$DOMINIO" ]; then
  echo "==> Configurando HTTPS para $DOMINIO"
  echo "${DOMINIO} {
    reverse_proxy localhost:8088
}" | sudo tee /etc/caddy/Caddyfile >/dev/null
  sudo systemctl reload caddy || sudo systemctl restart caddy
  echo "==> Listo: https://${DOMINIO}  (recuerda apuntar el registro A a esta IP)"
else
  echo "==> Sin dominio: la app queda en http://localhost:8088 (pásame el dominio como 1er argumento para HTTPS)"
fi

echo "==> Hecho. Revisa: pm2 logs finanzas-app finanzas-whatsapp"
