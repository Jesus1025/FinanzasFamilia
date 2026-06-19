@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo ============================================
echo   Finanzas Familia - Instalacion
echo ============================================
echo.
echo [1/5] Creando entorno Python (.venv)...
python -m venv .venv
echo [2/5] Instalando dependencias Python...
call .venv\Scripts\python.exe -m pip install --upgrade pip
call .venv\Scripts\python.exe -m pip install -r requirements.txt
echo [3/5] Instalando puente WhatsApp (Node)...
rem Usamos el Edge/Chrome del sistema; no hace falta que puppeteer baje Chromium.
set PUPPETEER_SKIP_DOWNLOAD=true
cd whatsapp-bridge
call npm install --no-audit --no-fund
cd ..
echo [4/5] Creando .env (si no existe)...
if not exist .env copy .env.example .env
echo [5/5] Cargando datos de ejemplo...
call .venv\Scripts\python.exe scripts\seed.py
echo.
echo ============================================
echo   Listo. Ahora ejecuta  Iniciar.cmd
echo ============================================
pause
