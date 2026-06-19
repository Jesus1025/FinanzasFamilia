@echo off
echo Deteniendo Finanzas Familia...
pm2 stop finanzas-app finanzas-whatsapp
pm2 delete finanzas-app finanzas-whatsapp
pm2 save
echo Listo.
pause
