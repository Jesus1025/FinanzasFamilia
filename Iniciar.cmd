@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
echo Iniciando Finanzas Familia (app + puente WhatsApp) con PM2...
pm2 start ecosystem.config.js
pm2 save
echo.
echo Dashboard:  http://localhost:8088
echo Conectar WhatsApp:  http://localhost:8088/whatsapp
start "" http://localhost:8088
echo.
echo (Cierra esta ventana cuando quieras; los procesos siguen vivos con PM2)
echo Mostrando logs... Ctrl+C para salir de los logs.
pm2 logs finanzas-app finanzas-whatsapp
