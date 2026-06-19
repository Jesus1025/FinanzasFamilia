// PM2: mantiene viva la app de Finanzas Familia y la relanza al encender el
// equipo / VPS. Solo la app web (FastAPI/uvicorn) en el puerto 8088.
// El puente WhatsApp fue reemplazado por Telegram (Bot API oficial, sin Chromium).
//
// Comandos:
//   pm2 start ecosystem.config.js   -> arranca
//   pm2 status                      -> estado
//   pm2 logs finanzas-app           -> logs en vivo
//   pm2 restart finanzas-app
//   pm2 stop finanzas-app
//   pm2 save                        -> recordar para el proximo arranque
const path = require("path");
const ROOT = __dirname;

const PYTHON = process.platform === "win32"
  ? path.join(ROOT, ".venv", "Scripts", "python.exe")
  : path.join(ROOT, ".venv", "bin", "python");

module.exports = {
  apps: [
    {
      name: "finanzas-app",
      script: "run.py",
      interpreter: PYTHON,
      cwd: ROOT,
      autorestart: true,
      max_restarts: 20,
      restart_delay: 3000,
      max_memory_restart: "400M",
      out_file: path.join(ROOT, "logs", "app.out.log"),
      error_file: path.join(ROOT, "logs", "app.err.log"),
      time: true,
      env: { PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
    },
  ],
};
