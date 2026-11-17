import asyncio
import logging
import os

import yfinance as yf
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
from prompts import SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Configuración (todo sale del .env)
# ---------------------------------------------------------------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL")

if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, GEMINI_MODEL]):
    raise RuntimeError(
        "Faltan variables en el .env (TELEGRAM_TOKEN, GEMINI_API_KEY, GEMINI_MODEL). "
        "Copiá .env.example como .env y completalo."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # menos ruido en consola
logger = logging.getLogger("FinanzasBot")

gemini = genai.Client(api_key=GEMINI_API_KEY)

# Cantidad máxima de mensajes del historial que se mandan a Gemini
MAX_HISTORIAL = 40
TELEGRAM_MAX_LEN = 4000

# ---------------------------------------------------------------------------
# Datos de mercado
# ---------------------------------------------------------------------------
ACCIONES_AR = [
    "GGAL.BA", "YPFD.BA", "PAMP.BA", "BMA.BA", "TXAR.BA",
    "ALUA.BA", "CEPU.BA", "EDN.BA", "TGSU2.BA", "TGNO4.BA",
    "COME.BA", "BBAR.BA", "SUPV.BA", "CRES.BA", "MIRG.BA",
    "LOMA.BA", "VALO.BA", "BYMA.BA", "TRAN.BA", "METR.BA",
]

CEDEARS_AR = [
    "AAPL.BA", "MSFT.BA", "GOOGL.BA", "AMZN.BA", "TSLA.BA",
    "NVDA.BA", "META.BA", "MELI.BA", "KO.BA", "PEP.BA",
    "DIS.BA", "NFLX.BA", "AMD.BA", "INTC.BA", "BABA.BA",
    "NU.BA", "V.BA", "JPM.BA", "WMT.BA", "SPY.BA",
]

# Montos mínimos según la planilla del proyecto
MONTO_MIN_ACCIONES = 50_000
MONTO_MIN_CEDEARS = 1_000_000


def calcular_top_10(lista_tickers, monto_minimo):
    resultados = []
    data = yf.download(
        lista_tickers,
        period="5d",  # 5 días para cubrir fines de semana y feriados
        group_by="ticker",
        progress=False,
        threads=True,
        auto_adjust=False,
    )

    for ticker in lista_tickers:
        try:
            df = data[ticker].dropna()
            if len(df) < 2:
                continue

            ultimo = float(df["Close"].iloc[-1])
            anterior = float(df["Close"].iloc[-2])
            volumen = float(df["Volume"].iloc[-1])

            variacion = ((ultimo - anterior) / anterior) * 100
            monto_operado = ultimo * volumen  # Estimación en ARS

            if monto_operado > monto_minimo and variacion > 0:
                resultados.append({
                    "ticker": ticker.replace(".BA", ""),
                    "ultimo": ultimo,
                    "variacion": variacion,
                    "monto": monto_operado,
                })
        except Exception:
            continue

    resultados.sort(key=lambda x: x["variacion"], reverse=True)
    return resultados[:10]


def obtener_detalle(simbolo):
    hist = yf.Ticker(simbolo).history(period="5d")
    if hist.empty:
        return None

    ultimo = float(hist["Close"].iloc[-1])
    cierre_ant = float(hist["Close"].iloc[-2]) if len(hist) > 1 else ultimo
    volumen = float(hist["Volume"].iloc[-1])
    return {
        "ultimo": ultimo,
        "cierre_ant": cierre_ant,
        "variacion": ((ultimo - cierre_ant) / cierre_ant) * 100,
        "apertura": float(hist["Open"].iloc[-1]),
        "minimo": float(hist["Low"].iloc[-1]),
        "maximo": float(hist["High"].iloc[-1]),
        "monto": ultimo * volumen,
    }


def formatear_top(titulo, top):
    texto = f"{titulo}\n\n"
    for i, item in enumerate(top, 1):
        texto += (
            f"{i}. *{item['ticker']}*: ${item['ultimo']:,.2f} "
            f"(+{item['variacion']:.2f}%) | Monto: ${item['monto']:,.0f}\n"
        )
    return texto


# ---------------------------------------------------------------------------
# Gemini (Interactions API, sin estado: el historial lo guardamos en SQLite)
# ---------------------------------------------------------------------------
def consultar_gemini(historial):
    interaction = gemini.interactions.create(
        model=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
        input=historial,
        store=False,  # sin estado: no se guarda nada del lado de Google
    )
    return (interaction.output_text or "").strip()


async def enviar_texto(update: Update, texto: str):
    """Manda texto con formato de Telegram; si el Markdown viene roto,
    lo manda como texto plano. Parte mensajes muy largos."""
    partes = [texto[i:i + TELEGRAM_MAX_LEN] for i in range(0, len(texto), TELEGRAM_MAX_LEN)]
    for parte in partes:
        try:
            await update.message.reply_text(parte, parse_mode="Markdown")
        except BadRequest:
            await update.message.reply_text(parte)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    mensaje = (
        f"¡Hola, {user_name}! 📈 Bienvenido a *FinanzasBot*.\n\n"
        "Consulto cotizaciones en pesos argentinos (ARS) vía Yahoo Finance:\n"
        "• /top\\_acciones - Top 10 acciones con mayor suba diaria (monto > $50.000)\n"
        "• /top\\_cedears - Top 10 CEDEARs con mayor suba diaria (monto > $1M)\n"
        "• `/precio TICKER` - Detalle de un activo (ej: `/precio GGAL`)\n"
        "• /borrar - Borra el historial de nuestra conversación\n\n"
        "💬 También podés escribirme sin comandos y charlamos sobre inversiones. "
        "Contame tu perfil (conservador, moderado, agresivo) y tus objetivos para "
        "que te responda a tu medida."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")


async def top_acciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando acciones líderes en ARS...")
    try:
        top = await asyncio.to_thread(calcular_top_10, ACCIONES_AR, MONTO_MIN_ACCIONES)
    except Exception as e:
        logger.exception("Error en top_acciones")
        await update.message.reply_text(f"⚠️ Error al consultar Yahoo Finance: {e}")
        return

    if not top:
        await update.message.reply_text(
            "No se encontraron acciones con variación positiva y monto > $50.000."
        )
        return

    await update.message.reply_text(
        formatear_top("🚀 *Top 10 Acciones Argentinas (ARS):*", top),
        parse_mode="Markdown",
    )


async def top_cedears(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analizando CEDEARs en ARS...")
    try:
        top = await asyncio.to_thread(calcular_top_10, CEDEARS_AR, MONTO_MIN_CEDEARS)
    except Exception as e:
        logger.exception("Error en top_cedears")
        await update.message.reply_text(f"⚠️ Error al consultar Yahoo Finance: {e}")
        return

    if not top:
        await update.message.reply_text(
            "No se encontraron CEDEARs con variación positiva y monto > $1.000.000."
        )
        return

    await update.message.reply_text(
        formatear_top("🌎 *Top 10 CEDEARs (ARS):*", top),
        parse_mode="Markdown",
    )


async def precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Indicá un ticker. Ejemplo: `/precio GGAL`", parse_mode="Markdown"
        )
        return

    simbolo = context.args[0].upper()
    if not simbolo.endswith(".BA"):
        simbolo += ".BA"
    nombre = simbolo.replace(".BA", "")

    try:
        d = await asyncio.to_thread(obtener_detalle, simbolo)
    except Exception as e:
        logger.exception("Error en precio")
        await update.message.reply_text(f"⚠️ Error al consultar: {e}")
        return

    if d is None:
        await update.message.reply_text(
            f"❌ No encontré datos en pesos para `{nombre}`.", parse_mode="Markdown"
        )
        return

    respuesta = (
        f"📊 *Detalle de {nombre} (ARS)*\n\n"
        f"• *Último operado:* ${d['ultimo']:,.2f}\n"
        f"• *Variación diaria:* {d['variacion']:+.2f}%\n"
        f"• *Apertura:* ${d['apertura']:,.2f}\n"
        f"• *Mínimo:* ${d['minimo']:,.2f}\n"
        f"• *Máximo:* ${d['maximo']:,.2f}\n"
        f"• *Último cierre:* ${d['cierre_ant']:,.2f}\n"
        f"• *Monto operado:* ${d['monto']:,.0f}"
    )
    await update.message.reply_text(respuesta, parse_mode="Markdown")


async def borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cantidad = database.borrar_historial(update.effective_chat.id)
    await update.message.reply_text(
        f"🗑️ Listo, borré nuestra conversación ({cantidad} mensajes). Empezamos de cero."
    )


# ---------------------------------------------------------------------------
# Chat libre con Gemini (cualquier texto que no sea un comando)
# ---------------------------------------------------------------------------
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto_usuario = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    database.guardar_mensaje(chat_id, "user", texto_usuario)
    historial = database.obtener_historial(chat_id, MAX_HISTORIAL)

    try:
        respuesta = await asyncio.to_thread(consultar_gemini, historial)
    except Exception:
        logger.exception("Error al consultar Gemini")
        # Sacamos el mensaje del usuario para no dejar el historial desparejo
        database.borrar_ultimo_mensaje(chat_id)
        await update.message.reply_text(
            "⚠️ No pude conectarme con la IA en este momento. Probá de nuevo en un rato."
        )
        return

    if not respuesta:
        respuesta = "No pude generar una respuesta. ¿Podés reformular la pregunta?"

    database.guardar_mensaje(chat_id, "model", respuesta)
    await enviar_texto(update, respuesta)


# ---------------------------------------------------------------------------
def main():
    database.inicializar_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("top_acciones", top_acciones))
    app.add_handler(CommandHandler("top_cedears", top_cedears))
    app.add_handler(CommandHandler("precio", precio))
    app.add_handler(CommandHandler("borrar", borrar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    print(f"🤖 FinanzasBot en funcionamiento (modelo: {GEMINI_MODEL})...")
    app.run_polling()


if __name__ == "__main__":
    main()
