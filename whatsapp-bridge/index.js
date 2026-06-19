/**
 * Puente WhatsApp (sesion unica) para Finanzas Familia.
 *
 * Un solo numero "bot" al que la familia le escribe sus gastos. El puente
 * mantiene la sesion de whatsapp-web.js (login por QR, persistido con LocalAuth)
 * y reenvia cada mensaje entrante a la app Python:
 *
 *   POST {API_URL}/webhook/bridge   { from, message, wa_message_id }  -> { reply }
 *
 * Tambien expone una API HTTP (protegida por token) para la web y el scheduler:
 *   POST /start            -> inicia/asegura la sesion
 *   GET  /estado           -> { estado, qr, numero }
 *   POST /logout           -> cierra sesion (pedira QR de nuevo)
 *   POST /send { to, message } -> envio proactivo (recordatorios)
 *
 * Variables de entorno:
 *   FIN_API_URL   URL de la app Python   (default http://localhost:8080)
 *   BRIDGE_TOKEN  token compartido        (default bridge-dev-token)
 *   BRIDGE_PORT   puerto del puente        (default 8090)
 *   CHROME_PATH   ruta a Chrome/Edge (autodetectado si no se define)
 */
import express from "express";
import fs from "fs";
import qrcode from "qrcode";
import pkg from "whatsapp-web.js";

const { Client, LocalAuth } = pkg;

const API_URL = (process.env.FIN_API_URL || "http://localhost:8088").replace(/\/$/, "");
const BRIDGE_TOKEN = process.env.BRIDGE_TOKEN || "bridge-dev-token";
const PORT = parseInt(process.env.BRIDGE_PORT || "8099", 10);

function detectarNavegador() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const candidatos = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ];
  return candidatos.find((p) => fs.existsSync(p)) || undefined;
}
const EXECUTABLE_PATH = detectarNavegador();

// --- Estado de la unica sesion -----------------------------------------------
let client = null;
let estado = "desconectado"; // desconectado | iniciando | qr | conectado | error
let qrDataUrl = null;
let numero = "";

function crearCliente() {
  estado = "iniciando";
  qrDataUrl = null;

  client = new Client({
    authStrategy: new LocalAuth({ clientId: "finanzas", dataPath: "./.wwebjs_auth" }),
    puppeteer: {
      headless: true,
      executablePath: EXECUTABLE_PATH,
      // Flags de bajo consumo: clave para correr en un VPS de 1 GB de RAM.
      args: [
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-extensions", "--disable-background-networking", "--disable-default-apps",
        "--disable-sync", "--disable-translate", "--no-first-run", "--mute-audio",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--disable-features=site-per-process,TranslateUI", "--js-flags=--max-old-space-size=256",
      ],
    },
  });

  client.on("qr", async (qr) => {
    estado = "qr";
    try { qrDataUrl = await qrcode.toDataURL(qr, { width: 300 }); } catch { qrDataUrl = null; }
    console.log("QR generado, escanealo desde la web.");
  });

  client.on("ready", async () => {
    estado = "conectado";
    qrDataUrl = null;
    numero = client.info?.wid?.user || "";
    console.log(`✓ Conectado como ${numero}`);
    await reportarEstado(true, numero);
  });

  client.on("authenticated", () => console.log("Autenticado."));
  client.on("auth_failure", (m) => { estado = "error"; console.error("Fallo de autenticacion:", m); });

  client.on("disconnected", async (reason) => {
    estado = "desconectado";
    console.log("Desconectado:", reason);
    await reportarEstado(false, numero);
  });

  // Solo mensajes ENTRANTES (no grupos, no estados, no propios).
  client.on("message", async (msg) => {
    if (msg.from.endsWith("@g.us") || msg.from === "status@broadcast" || msg.fromMe) return;
    if (!msg.body || !msg.body.trim()) return;
    // WhatsApp ahora puede entregar un "@lid" (id de privacidad) en vez del número
    // real. Resolvemos el teléfono de verdad (pn) a partir del LID; si falla,
    // probamos el contacto y por último caemos al id crudo del remitente.
    let telefono = msg.from.split("@")[0];
    try {
      if (msg.from.endsWith("@lid")) {
        const mapa = await client.getContactLidAndPhone([msg.from]);
        const pn = mapa && mapa[0] && mapa[0].pn;
        if (pn) telefono = String(pn).split("@")[0].replace(/\D/g, "");
      } else {
        const contact = await msg.getContact();
        const real = contact && (contact.number || (contact.id && contact.id.user));
        if (real) telefono = String(real).replace(/\D/g, "");
      }
    } catch (e) {
      console.warn("No pude resolver el número real del remitente:", e.message);
    }
    console.log(`← ${telefono} (from=${msg.from}): ${msg.body}`);
    try {
      const resp = await fetch(`${API_URL}/webhook/bridge`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bridge-Token": BRIDGE_TOKEN },
        body: JSON.stringify({
          from: telefono,
          message: msg.body,
          wa_message_id: msg.id ? msg.id._serialized : null,
        }),
      });
      const data = await resp.json();
      if (data && data.reply) {
        await msg.reply(data.reply);
        console.log(`→ ${telefono}: ${data.reply.slice(0, 60)}`);
      }
    } catch (e) {
      console.error("Error hablando con Python:", e.message);
    }
  });

  client.initialize().catch((e) => {
    estado = "error";
    console.error("Error al inicializar:", e.message);
    // Navegador en estado irrecuperable (lock/zombie/frame muerto): la unica
    // salida limpia es reiniciar el proceso. Bajo PM2 (prod) se relanza solo y
    // el nuevo proceso si puede abrir el userDataDir. En local se loguea para
    // reiniciar a mano (no matamos el proceso sin un supervisor que lo reviva).
    if (/already running|Target closed|detached|Session closed/i.test(e.message || "")) {
      if (process.env.pm_id !== undefined) {
        console.error("Reiniciando el proceso para recuperar la sesion...");
        setTimeout(() => process.exit(1), 1500);
      } else {
        console.error("Sesion trabada. Reinicia el puente (o usa PM2 para que se recupere solo).");
      }
    }
  });
}

async function reportarEstado(conectado, num) {
  try {
    await fetch(`${API_URL}/webhook/bridge/estado`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Token": BRIDGE_TOKEN },
      body: JSON.stringify({ conectado, numero: num }),
    });
  } catch (e) { /* la app puede no estar lista todavia */ }
}

// --- API HTTP del puente -----------------------------------------------------
const app = express();
app.use(express.json());

function auth(req, res, next) {
  if (req.headers["x-bridge-token"] !== BRIDGE_TOKEN) return res.status(403).json({ error: "token invalido" });
  next();
}

app.post("/start", auth, async (_req, res) => {
  if (!client || estado === "desconectado" || estado === "error") crearCliente();
  res.json({ estado });
});

app.get("/estado", auth, (_req, res) => {
  res.json({ estado, qr: qrDataUrl, numero });
});

app.post("/logout", auth, async (_req, res) => {
  try { if (client) await client.logout(); } catch (e) { try { await client.destroy(); } catch {} }
  client = null; estado = "desconectado"; qrDataUrl = null; numero = "";
  await reportarEstado(false, "");
  res.json({ ok: true });
});

app.post("/send", auth, async (req, res) => {
  const { to, message } = req.body || {};
  if (!to || !message) return res.status(400).json({ ok: false, error: "faltan to/message" });
  if (!client || estado !== "conectado") return res.json({ ok: false, error: "sesion no conectada" });
  const digits = String(to).replace(/\D/g, "");
  const chatId = digits.includes("@") ? to : `${digits}@c.us`;
  try {
    await client.sendMessage(chatId, String(message));
    res.json({ ok: true });
  } catch (e) {
    console.error("Error enviando:", e.message);
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.get("/", (_req, res) => res.send("Finanzas Familia · puente WhatsApp activo."));

app.listen(PORT, () => {
  console.log(`Puente en http://localhost:${PORT}  ->  app Python: ${API_URL}`);
  if (EXECUTABLE_PATH) console.log("Navegador:", EXECUTABLE_PATH);
  else console.warn("No se detecto Chrome/Edge. Define CHROME_PATH si el QR no aparece.");
  // Restaura la sesion previa (LocalAuth) automaticamente al arrancar.
  crearCliente();
  iniciarWatchdog();
});

// Watchdog: si Chromium muere sin avisar (OOM en VPS chico), recrea la sesion.
// `recreando` evita que dos ciclos intenten recrear a la vez (causaba el choque
// "browser is already running" sobre el mismo userDataDir).
let recreando = false;
function iniciarWatchdog(intervaloMs = 60000) {
  setInterval(async () => {
    if (!client || estado !== "conectado" || recreando) return;
    try {
      await client.getState();
    } catch (e) {
      console.warn("Watchdog: sesion caida, recreando:", e.message);
      recreando = true;
      try { await client.destroy(); } catch {}
      await new Promise((r) => setTimeout(r, 2000));  // deja que Chromium libere el lock
      client = null;
      try { crearCliente(); } catch (err) { console.error("No pude recrear:", err.message); }
      recreando = false;
    }
  }, intervaloMs);
}

// Un error no capturado NO debe matar el puente (sobre todo en el VPS de 1 GB,
// donde Chromium puede morir feo). Logueamos y seguimos; el watchdog reconecta.
process.on("unhandledRejection", (e) => console.error("unhandledRejection:", (e && e.message) || e));
process.on("uncaughtException", (e) => console.error("uncaughtException:", (e && e.message) || e));
