# Despliegue en un VPS (Debian/Ubuntu)

Pensado para un VPS chico (1 vCPU / 2 GB, ~$4.000 CLP). Mismo enfoque que el
99520: **PM2** mantiene vivos la app Python y el puente Node; **Chromium del
sistema** corre whatsapp-web.js. Funciona detrás de un dominio con HTTPS.

> 1 GB de RAM funciona pero **al límite** por el Chromium del puente; el puente
> ya viene con flags de bajo consumo y el script crea 2 GB de swap. **2 GB es lo
> recomendado.**

## Atajo: instalación automática (Debian 12)
Sube el proyecto al VPS y, desde su carpeta, corre:
```bash
bash scripts/deploy-debian.sh finanzas.divergentstudio.cl
```
Hace todo lo de abajo: swap, dependencias (python/node/chromium/caddy), `.env`
con secretos generados, `npm install`, PM2 con arranque automático y HTTPS para
tu dominio. Solo te queda **editar `.env`** (DEEPSEEK_API_KEY, GOOGLE_CLIENT_ID/
SECRET, SUPER_ADMIN_EMAIL, APP_URL) y `pm2 restart all`. Para hacerlo a mano:

## 0. Antes de empezar
- Un dominio apuntando (registro A) a la IP del VPS, p. ej. `finanzas.tudominio.cl`.
- Un **número de WhatsApp** dedicado para el bot (idealmente no tu personal).

## 1. Endurecer el VPS
```bash
adduser finanzas && usermod -aG sudo finanzas    # usuario no-root
# entra como 'finanzas' y usa llaves SSH (deshabilita login por password)
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
# swap de 2 GB (clave con 2 GB de RAM)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Dependencias
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git chromium
# Node 20+ (NodeSource) y PM2:
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs
sudo npm install -g pm2
```
> En Debian el binario suele ser `/usr/bin/chromium`; en Ubuntu `/usr/bin/chromium-browser`.
> El puente lo autodetecta. Si no, exporta `CHROME_PATH` en el `env` de PM2.

## 3. Subir el proyecto e instalar
```bash
cd ~ && git clone <tu-repo> finanzas-familia   # o sube la carpeta por scp/rsync
cd finanzas-familia
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env              # edita SECRET_KEY, DEEPSEEK_API_KEY, BRIDGE_TOKEN
.venv/bin/python scripts/seed.py               # crea la familia (edita telefonos reales)
cd whatsapp-bridge && npm install && cd ..
```
Edita `.env` con valores de producción:
```
ENV=production
SECRET_KEY=<openssl rand -hex 32>
WHATSAPP_BRIDGE_TOKEN=<un token largo y secreto>
DEEPSEEK_API_KEY=<tu key>   # opcional
```
Y en `ecosystem.config.js` cambia `BRIDGE_TOKEN` para que coincida con el `.env`.

## 4. Arrancar con PM2 (y que reviva al reiniciar)
```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup        # ejecuta la línea que imprime (systemd)
pm2 logs finanzas-app finanzas-whatsapp
```

## 5. HTTPS con Caddy (TLS automático, sin configurar certificados)
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```
`/etc/caddy/Caddyfile`:
```
finanzas.tudominio.cl {
    reverse_proxy localhost:8088
}
```
```bash
sudo systemctl reload caddy
```
Listo: entra a `https://finanzas.tudominio.cl`, pestaña **WhatsApp**, escanea el QR.
El puente (`:8099`) queda solo en localhost — **no lo expongas**.

## 6. Conectar el WhatsApp del bot
La primera vez aparece un QR en el dashboard. Escanéalo con el número del bot
(WhatsApp → Dispositivos vinculados). LocalAuth guarda la sesión: si el VPS se
reinicia, PM2 + el watchdog reconectan solos.

## Seguridad mínima
- `WHATSAPP_BRIDGE_TOKEN` largo y secreto (protege el webhook y el `/send`).
- Solo los teléfonos dados de alta (`is_active`) pueden registrar gastos.
- Postgres/MariaDB (si migras) sin puerto público.
- Respaldo diario de la BD: `cron` con `cp finanzas.db backups/` (o `mysqldump`).

## Crecer
- Más RAM (4 GB) cuando entren más familias.
- SQLite → MariaDB: cambia `DATABASE_URL` y corre el seed. El esquema ya es
  multi-familia (todo cuelga de `household_id`).
