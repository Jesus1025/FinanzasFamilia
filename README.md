# 💸 Finanzas Familia

Gestor de finanzas **multi-familia** que se controla **por WhatsApp** y desde un
**dashboard web** con asistente IA. Cada persona le escribe sus gastos al bot en
lenguaje natural y el **agente IA (DeepSeek con function calling)** los entiende,
los registra y responde con sus números reales: cuánto entra, cuánto se gasta,
cuánto va a sobrar, gastos hormiga, presupuestos y recordatorios de cuentas.

```
WhatsApp ──► puente Node (whatsapp-web.js, :8099) ──HTTP──► FastAPI (:8088) ──► SQLite
                                                              │
                                                              ├─► Agente DeepSeek (tools: registrar, consultar, presupuestar…)
                                                              └─► Dashboard web (gráficos en vivo + chat + análisis IA) + Excel
```

## El agente IA (con `DEEPSEEK_API_KEY`)
- **Registra de verdad**: "gasté 12 lucas en bencina y 5 de café" → 2 movimientos
  en la base de datos, con confirmación y acumulado del mes.
- **Memoria**: "uy, eran 35 lucas, corrígelo" → borra el anterior y registra el nuevo.
- **Consulta datos reales** (nunca inventa): "¿cuánto llevo gastado yo?" responde
  con los números de ESA persona, sacados con herramientas.
- **Presupuestos**: "ponle 100 lucas de tope al delivery" → presupuesto con barra
  en el dashboard.
- **Análisis del mes**: el panel "🧠 Análisis IA" del dashboard se genera con tus
  datos y se cachea (se regenera cuando cambian los movimientos).
- **Chat web flotante**: el mismo agente, en el dashboard (botón 🪙 abajo a la derecha).

Sin API key todo sigue funcionando con un parser local de reglas (modo básico).

## Más funciones
- 🔐 **Login con Google** (OAuth): cualquiera entra y queda *pendiente* hasta que el super admin lo aprueba y lo conecta a su familia (ver abajo).
- 📅 **Google Calendar**: "recuérdame el martes pagar el arriendo" agenda un evento con alarma; al marcarlo pagado, se borra.
- 🔔 **Alertas de sobregasto**: cuando un gasto cruza el presupuesto de una categoría, el bot avisa al instante por WhatsApp.
- ✏️ **Editar/eliminar movimientos** desde el dashboard.
- 📊 **Exportar a Excel** (reporte con resumen, movimientos y cuentas) y **a PDF** imprimible.
- 📱 **Instalable como app (PWA)** en el celular, con ícono propio.
- 🪙 **Asistente con nombre propio por familia** (configurable en `/admin`).

## Multi-familia (nivel enterprise casero 😄)
- El **super admin** entra a **`/admin`** con `ADMIN_PASSWORD` y crea familias
  ("Familia González", "Familia Leyton"…), cada una con sus categorías propias.
- A cada familia le agrega **perfiles** con su **teléfono de WhatsApp**: el bot
  reconoce a cada persona por su número y registra TODO en su familia, aislado
  de las demás.
- El admin cambia de familia desde el selector del dashboard (`/?hh=ID`).
- Un solo número "bot" de WhatsApp atiende a todas las familias.

## Qué entiende (ejemplos por WhatsApp o chat web)
- `gasté 15 lucas en bencina` → registra gasto
- `pagué 38.000 en el super y 6 lucas de café` → registra ambos
- `me llegó un bono de 50 lucas` → ingreso
- `mi sueldo es 900 lucas` → fija tu sueldo mensual
- `el 20 pago el arriendo, 350 lucas` → recordatorio (avisa por WhatsApp antes)
- `¿cuánto llevo gastado?` / `¿cuánto nos queda?` / `gastos hormiga` → consultas
- `ponle un presupuesto de 100 lucas al delivery` → límite con alerta visual
- `bórrame el gasto del café` → busca y elimina

## Correr en local (Windows)
1. Doble clic en **`Instalar.cmd`** (crea el entorno, instala todo y carga datos demo).
2. Doble clic en **`Iniciar.cmd`** (levanta app + puente con PM2 y abre el dashboard).
3. Abre **http://localhost:8088** → pestaña **WhatsApp** → escanea el QR con el
   número que será el "bot" de las familias.
4. Entra a **/admin** (clave: `ADMIN_PASSWORD` del `.env`) y deja los teléfonos
   reales de cada perfil (formato `569XXXXXXXX`).
5. Para detener: **`Detener.cmd`**.

### A mano (sin los .cmd)
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env       # edita DEEPSEEK_API_KEY y ADMIN_PASSWORD
.venv\Scripts\python scripts\seed.py
cd whatsapp-bridge; npm install; cd ..
.venv\Scripts\python run.py            # app  -> http://localhost:8088
# en otra terminal:
node whatsapp-bridge\index.js          # puente -> :8099
```

## Configuración (`.env`)
| Variable | Para qué |
|---|---|
| `DEEPSEEK_API_KEY` | Activa el agente IA, el chat web y el análisis del mes. |
| `ADMIN_PASSWORD` | Clave del panel `/admin` (familias y perfiles). |
| `WHATSAPP_BRIDGE_TOKEN` | Token compartido app ↔ puente (cámbialo en prod). |
| `DATABASE_URL` | SQLite por defecto; puedes apuntar a MariaDB en el VPS. |
| `SECRET_KEY` | Secreto de sesión (genera uno en prod). |

## Estructura
```
app/
  services/ia.py        Agente DeepSeek (function calling) + insights + memoria
  services/asistente.py Orquestador WhatsApp (IA con fallback heurístico)
  services/finanzas.py  Lógica de negocio (resúmenes, series, presupuestos…)
  routers/admin.py      Panel super admin (familias y perfiles)
  routers/chat.py       Chat web + API JSON del dashboard
  routers/dashboard.py  Dashboard, setup, formularios
whatsapp-bridge/        Puente Node (whatsapp-web.js)
templates/ static/      UI (dashboard en vivo, chat flotante, admin)
scripts/seed.py         Datos de ejemplo
DEPLOY.md               Despliegue en VPS
```

Ver **DEPLOY.md** para subirlo a un VPS.
