import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import List, Literal

from dotenv import load_dotenv

# El .env se busca siempre en la carpeta de este archivo
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH, override=True)

from google import genai
from pydantic import BaseModel, Field
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import finanzas
from finanzas import formato_numero, formato_pct, nombre_corto
from prompts import SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL")

_faltantes = [
    nombre for nombre, valor in {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "GEMINI_MODEL": MODEL,
    }.items() if not valor
]
if _faltantes:
    raise RuntimeError(
        f"Faltan variables: {', '.join(_faltantes)}. "
        f"Busqué el archivo en: {ENV_PATH} (¿existe? {ENV_PATH.exists()})"
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("FinanzasBot")

client = genai.Client(api_key=GEMINI_API_KEY)

MAX_HISTORIAL = 40
TELEGRAM_MAX_LEN = 4000
COMISION = 0.005  # 0,5% estimado


# ---------------------------------------------------------------------------
# ESQUEMA PYDANTIC PARA SALIDA ESTRUCTURADA
# ---------------------------------------------------------------------------
class AnalisisConsulta(BaseModel):
    respuesta_usuario: str = Field(
        description="Respuesta clara en texto plano para enviar al usuario en Telegram"
    )
    perfil_detectado: Literal["conservador", "moderado", "agresivo", "no_especificado"] = Field(
        description="Perfil de inversor detectado en la conversación"
    )
    intencion: Literal["cotizacion", "ranking", "educacion_financiera", "cartera", "fuera_de_tema"] = Field(
        description="Categoría principal de la consulta del usuario"
    )
    activos_mencionados: List[str] = Field(
        default_factory=list,
        description="Lista de tickers mencionados (ej. ['NVDA.BA', 'GGAL.BA'])"
    )
    nivel_riesgo: Literal["bajo", "medio", "alto", "no_aplica"] = Field(
        description="Nivel de riesgo asociado a la consulta o activos tratados"
    )


# ---------------------------------------------------------------------------
# MENÚS DE TELEGRAM
# ---------------------------------------------------------------------------
BTN_TOP_ACCIONES = "📈 Top acciones"
BTN_TOP_CEDEARS = "🌎 Top CEDEARs"
BTN_BUSCAR = "🔎 Buscar activo"
BTN_NOTICIAS = "📰 Noticias"
BTN_SIMULAR = "🧮 Simular inversión"
BTN_MIS_SIMULACIONES = "📂 Mis simulaciones"
BTN_CHAT = "💬 Consultar a FinanzasBot"
BTN_BORRAR = "🗑️ Borrar conversación"
BTN_VOLVER = "⬅️ Volver al menú"

BTN_ACCION_AR = "Acción Argentina"
BTN_CEDEAR = "CEDEAR"
BTN_CANCELAR_SIM = "❌ Cancelar simulación"
BTN_CONFIRMAR_SIM = "✅ Confirmar simulación"


def _teclado(filas):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t) for t in fila] for fila in filas], resize_keyboard=True
    )


def obtener_menu():
    return _teclado([
        [BTN_TOP_ACCIONES, BTN_TOP_CEDEARS],
        [BTN_BUSCAR, BTN_NOTICIAS],
        [BTN_SIMULAR, BTN_MIS_SIMULACIONES],
        [BTN_CHAT, BTN_BORRAR],
    ])


def obtener_menu_volver():
    return _teclado([[BTN_VOLVER]])


def obtener_menu_tipo_activo():
    return _teclado([[BTN_ACCION_AR, BTN_CEDEAR], [BTN_CANCELAR_SIM]])


def obtener_menu_cancelar_flujo():
    return _teclado([[BTN_CANCELAR_SIM]])


def obtener_menu_confirmar_flujo():
    return _teclado([[BTN_CONFIRMAR_SIM, BTN_CANCELAR_SIM]])


async def responder(update: Update, texto: str, teclado=None):
    """Manda texto plano; si es muy largo lo parte en varios mensajes."""
    partes = [texto[i:i + TELEGRAM_MAX_LEN] for i in range(0, len(texto), TELEGRAM_MAX_LEN)] or [""]
    for i, parte in enumerate(partes):
        es_ultima = i == len(partes) - 1
        await update.message.reply_text(
            parte,
            reply_markup=teclado if es_ultima else None,
            disable_web_page_preview=True,
        )


# ---------------------------------------------------------------------------
# GEMINI (Interactions API, sin estado) + DATOS DE YAHOO + SALIDA ESTRUCTURADA
# ---------------------------------------------------------------------------
def armar_turnos(historial):
    """[{"role": "user"/"model", "content": texto}] -> steps de la Interactions API."""
    turnos = []
    for h in historial:
        tipo = "user_input" if h["role"] == "user" else "model_output"
        turnos.append({
            "type": tipo,
            "content": [{"type": "text", "text": h["content"]}]
        })
    return turnos


def _limpiar_json(texto):
    texto = texto.strip()
    bloque = re.search(r"\{.*\}", texto, re.DOTALL)
    return bloque.group(0) if bloque else texto


INSTRUCCION_JSON = (
    "\n\n# SALIDA ESTRUCTURADA OBLIGATORIA\n"
    "Debes responder ÚNICAMENTE con un objeto JSON válido (sin bloques markdown ```json) "
    "que cumpla exactamente con estas claves:\n"
    "{\n"
    '  "respuesta_usuario": "texto plano con tu respuesta siguiendo las reglas de formato",\n'
    '  "perfil_detectado": "conservador" | "moderado" | "agresivo" | "no_especificado",\n'
    '  "intencion": "cotizacion" | "ranking" | "educacion_financiera" | "cartera" | "fuera_de_tema",\n'
    '  "activos_mencionados": ["TICKER.BA"],\n'
    '  "nivel_riesgo": "bajo" | "medio" | "alto" | "no_aplica"\n'
    "}"
)


def preguntar_a_gemini_estructurado(historial, ultima_consulta=""):
    """Le pregunta a Gemini con el historial + los datos reales de Yahoo Finance
    y devuelve la respuesta validada con Pydantic. Reintenta hasta 3 veces."""
    ultimo_error = None

    # Datos reales de Yahoo Finance que Gemini usa para responder
    contexto_mercado = finanzas.obtener_contexto_mercado(ultima_consulta)
    instruccion_con_datos = f"{SYSTEM_PROMPT}\n\n{contexto_mercado}{INSTRUCCION_JSON}"

    for intento in range(3):
        try:
            interaccion = client.interactions.create(
                model=MODEL,
                input=armar_turnos(historial),
                system_instruction=instruccion_con_datos,
                store=False,  # sin estado: el historial vive en SQLite
            )

            texto_crudo = interaccion.output_text
            if not texto_crudo:
                raise ValueError("Gemini devolvió una respuesta vacía.")

            objeto_validado = AnalisisConsulta.model_validate_json(_limpiar_json(texto_crudo))

            print("\n=== [SALIDA ESTRUCTURADA VALIDADA CON PYDANTIC] ===")
            print(json.dumps(objeto_validado.model_dump(), indent=2, ensure_ascii=False))
            print("===================================================\n")

            return objeto_validado

        except Exception as error:
            ultimo_error = error
            logger.exception("Error de Gemini o validación Pydantic. Intento %s de 3", intento + 1)
            if intento < 2:
                time.sleep(2 * (intento + 1))

    raise ultimo_error


def preguntar_a_gemini(historial, ultima_consulta=""):
    return preguntar_a_gemini_estructurado(historial, ultima_consulta).respuesta_usuario


def resumir_noticias(simbolo, noticias):
    """Gemini resume en español las noticias que trajo Yahoo Finance."""
    texto = "\n\n".join(
        f"- {n['titulo']} ({n['fuente']})\n{n['resumen']}" for n in noticias
    )
    pedido = (
        f"Estas son las últimas noticias sobre {simbolo} (en inglés, de Yahoo Finance):\n\n"
        f"{texto}\n\n"
        "Resúmelas en español en 3 a 5 viñetas cortas con guiones y cierra con una línea "
        "sobre el posible impacto para un inversor. Usa solo esta información, no agregues datos. "
        "Responde en texto plano, no en JSON."
    )
    for intento in range(2):
        try:
            interaccion = client.interactions.create(
                model=MODEL,
                system_instruction=SYSTEM_PROMPT,
                input=pedido,
                store=False,
            )
            return (interaccion.output_text or "").strip()
        except Exception:
            logger.exception("Error al resumir noticias. Intento %s de 2", intento + 1)
            if intento == 0:
                time.sleep(2)
    return ""


# ---------------------------------------------------------------------------
# FUNCIONES DE DATOS (botones y comandos)
# ---------------------------------------------------------------------------
def _texto_top(titulo, activos, monto_minimo):
    texto = f"{titulo}\n\n"
    for i, a in enumerate(activos, start=1):
        texto += (
            f"{i}. {nombre_corto(a['ticker'])}: ${formato_numero(a['precio'])} "
            f"({formato_pct(a['variacion'])})\n"
            f"   Monto operado: ${formato_numero(a['monto_operado'], 0)}\n"
        )
    texto += (
        f"\nFiltro: suba diaria y monto operado > ${formato_numero(monto_minimo, 0)}.\n"
        f"Datos de la última rueda disponible ({activos[0]['fecha']})."
    )
    return texto


async def mostrar_top(update: Update, tipo: str):
    if tipo == "acciones":
        titulo, buscar, minimo = "🚀 TOP 10 ACCIONES ARGENTINAS (ARS)", finanzas.obtener_top_acciones, finanzas.MONTO_MIN_ACCIONES
        await update.message.reply_text("⏳ Analizando acciones líderes en ARS...")
    else:
        titulo, buscar, minimo = "🌎 TOP 10 CEDEARs (ARS)", finanzas.obtener_top_cedears, finanzas.MONTO_MIN_CEDEARS
        await update.message.reply_text("⏳ Analizando CEDEARs en ARS...")

    try:
        activos = await asyncio.to_thread(buscar)
    except Exception:
        logger.exception("Error consultando top %s", tipo)
        await responder(update, "No pude consultar Yahoo Finance. Probá nuevamente.", obtener_menu_volver())
        return

    if not activos:
        await responder(
            update,
            f"No se encontraron {tipo} con suba diaria y monto operado > ${formato_numero(minimo, 0)}.",
            obtener_menu_volver(),
        )
        return

    await responder(update, _texto_top(titulo, activos, minimo), obtener_menu_volver())


async def buscar_activo(update: Update, ticker: str):
    try:
        a = await asyncio.to_thread(finanzas.consultar_activo, ticker)
    except Exception:
        logger.exception("Error buscando activo")
        await responder(update, "Ocurrió un error al consultar ese activo.", obtener_menu_volver())
        return

    if a is None:
        await responder(
            update,
            f"No encontré datos en pesos para {nombre_corto(ticker)}. Revisá el símbolo.",
            obtener_menu_volver(),
        )
        return

    texto = (
        f"📊 DETALLE DE {nombre_corto(a['ticker'])} (ARS)\n\n"
        f"• Último operado: ${formato_numero(a['precio'])}\n"
        f"• Variación diaria: {formato_pct(a['variacion'])}\n"
        f"• Variación semanal: {formato_pct(a['variacion_semanal'])}\n"
        f"• Variación mensual: {formato_pct(a['variacion_mensual'])}\n"
        f"• Variación 3 meses: {formato_pct(a['variacion_3m'])}\n"
        f"• Apertura: ${formato_numero(a['apertura'])}\n"
        f"• Mínimo: ${formato_numero(a['minimo'])}\n"
        f"• Máximo: ${formato_numero(a['maximo'])}\n"
        f"• Último cierre: ${formato_numero(a['cierre_anterior'])}\n"
        f"• Volumen: {formato_numero(a['volumen'], 0)}\n"
        f"• Monto operado: ${formato_numero(a['monto_operado'], 0)}\n\n"
        f"Datos de la última rueda disponible ({a['fecha']})."
    )
    await responder(update, texto, obtener_menu_volver())


async def mostrar_noticias(update: Update, context: ContextTypes.DEFAULT_TYPE, ticker: str):
    await update.message.reply_text("📰 Buscando noticias...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        simbolo, lista = await asyncio.to_thread(finanzas.obtener_noticias, ticker)
    except Exception:
        logger.exception("Error en noticias")
        await responder(update, "No pude consultar las noticias. Probá nuevamente.", obtener_menu_volver())
        return

    if not lista:
        await responder(
            update,
            f"No encontré noticias recientes de {simbolo}. Las acciones que solo cotizan "
            "en BYMA (sin ADR en EE.UU.) suelen no tener noticias en Yahoo.",
            obtener_menu_volver(),
        )
        return

    resumen = await asyncio.to_thread(resumir_noticias, simbolo, lista)

    links = "\n".join(f"• {n['titulo']}\n  {n['url']}" for n in lista if n["url"])
    texto = (
        f"📰 NOTICIAS DE {simbolo}\n\n"
        f"{resumen or 'No pude generar el resumen, pero acá están las fuentes.'}\n\n"
        f"🔗 Fuentes:\n{links}"
    )

    # Se guarda en el historial para que el chat con la IA pueda usar estas noticias
    if resumen:
        chat_id = update.effective_chat.id
        db.guardar(chat_id, "user", f"Mostrame las noticias de {simbolo}")
        db.guardar(chat_id, "model", f"Noticias de {simbolo}:\n{resumen}")

    await responder(update, texto, obtener_menu_volver())


async def mostrar_simulaciones(update: Update):
    simulaciones = db.listar_simulaciones(update.effective_chat.id)
    if not simulaciones:
        await responder(
            update,
            f"Todavía no tenés simulaciones guardadas. Probá con {BTN_SIMULAR}.",
            obtener_menu_volver(),
        )
        return

    await update.message.reply_text("⏳ Calculando cómo vienen tus simulaciones...")

    tickers = sorted({s["ticker"] for s in simulaciones})
    precios = {}
    for t in tickers:
        try:
            datos = await asyncio.to_thread(finanzas.consultar_activo, t)
            precios[t] = datos["precio"] if datos else None
        except Exception:
            precios[t] = None

    texto = "📂 MIS SIMULACIONES (últimas 10)\n\n"
    total_invertido = total_actual = 0.0
    for i, s in enumerate(simulaciones, start=1):
        precio_hoy = precios.get(s["ticker"])
        texto += (
            f"{i}. {nombre_corto(s['ticker'])} ({s['tipo']}) - {s['fecha']}\n"
            f"   Invertido: ${formato_numero(s['monto'])} a ${formato_numero(s['precio_compra'])}\n"
        )
        if precio_hoy:
            valor_hoy = s["cantidad"] * precio_hoy
            resultado = valor_hoy - s["monto"]
            pct = resultado / s["monto"] * 100
            total_invertido += s["monto"]
            total_actual += valor_hoy
            texto += (
                f"   Hoy: ${formato_numero(precio_hoy)} → vale ${formato_numero(valor_hoy)}\n"
                f"   Resultado: {'+' if resultado >= 0 else '-'}${formato_numero(abs(resultado))} "
                f"({formato_pct(pct)})\n\n"
            )
        else:
            texto += "   Hoy: sin datos de Yahoo Finance\n\n"

    if total_invertido:
        resultado_total = total_actual - total_invertido
        texto += (
            f"TOTAL: invertido ${formato_numero(total_invertido)} → "
            f"hoy ${formato_numero(total_actual)} "
            f"({formato_pct(resultado_total / total_invertido * 100)})\n\n"
        )
    texto += "El resultado incluye la comisión estimada del 0,5%. Es una simulación, no una inversión real."
    await responder(update, texto, obtener_menu_volver())


# ---------------------------------------------------------------------------
# FLUJO GUIADO: SIMULADOR DE INVERSIÓN
# ---------------------------------------------------------------------------
def parsear_monto(texto):
    """Interpreta montos escritos de distintas formas. Devuelve float o None.
    '250000', '250.000', '$ 250.000', '1500,50', '1.500,50', '1500.50' """
    t = texto.replace("$", "").replace(" ", "").strip()
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")      # 1.500,50
    elif "," in t:
        t = t.replace(",", ".")                        # 1500,50
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")                         # 250.000 / 1.500.000
    try:
        return float(t)
    except ValueError:
        return None


async def manejar_flujo_simulacion(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    paso = context.user_data.get("flujo_paso")

    # Cancelación en cualquier punto del flujo
    if texto == BTN_CANCELAR_SIM or texto.lower() == "cancelar":
        context.user_data.clear()
        await update.message.reply_text(
            "❌ Simulación cancelada. Volviste al menú principal.",
            reply_markup=obtener_menu()
        )
        return

    # PASO 1: tipo de activo
    if paso == "tipo":
        if texto not in (BTN_ACCION_AR, BTN_CEDEAR):
            await update.message.reply_text(
                "⚠️ Opción inválida. Elegí 'Acción Argentina' o 'CEDEAR' usando los botones, o tocá Cancelar.",
                reply_markup=obtener_menu_tipo_activo()
            )
            return

        context.user_data["sim_tipo"] = texto
        context.user_data["flujo_paso"] = "ticker"
        ejemplos = "GGAL, YPFD, PAMP" if texto == BTN_ACCION_AR else "NVDA, AAPL, MELI"

        await update.message.reply_text(
            f"Paso 2 de 4: Elegiste {texto}.\n"
            f"Escribí el símbolo (ticker) del activo que querés simular (ejemplos: {ejemplos}):",
            reply_markup=obtener_menu_cancelar_flujo()
        )
        return

    # PASO 2: ticker válido y existente en Yahoo Finance
    if paso == "ticker":
        ticker = finanzas.normalizar_ticker(texto)
        if ticker is None:
            await update.message.reply_text(
                "⚠️ Formato de símbolo inválido. Ingresá solo las letras del ticker (ej. GGAL o NVDA):",
                reply_markup=obtener_menu_cancelar_flujo()
            )
            return

        await update.message.reply_text(f"Verificando cotización de {nombre_corto(ticker)}...")
        datos = await asyncio.to_thread(finanzas.consultar_activo, ticker)

        if datos is None:
            await update.message.reply_text(
                f"⚠️ No se encontró '{nombre_corto(ticker)}' en el mercado. Ingresá un símbolo válido (ej. AAPL, MELI, YPFD):",
                reply_markup=obtener_menu_cancelar_flujo()
            )
            return

        context.user_data["sim_ticker"] = ticker
        context.user_data["sim_precio"] = datos["precio"]
        context.user_data["flujo_paso"] = "monto"

        await update.message.reply_text(
            f"Paso 3 de 4: {nombre_corto(ticker)} cotiza a ${formato_numero(datos['precio'])}.\n"
            "Ingresá el monto en pesos argentinos que querés invertir (mínimo $1.000):",
            reply_markup=obtener_menu_cancelar_flujo()
        )
        return

    # PASO 3: monto numérico válido
    if paso == "monto":
        monto = parsear_monto(texto)
        if monto is None:
            await update.message.reply_text(
                "⚠️ Dato inválido: ingresá un número sin letras (ejemplo: 250000 o 250.000). Intentá de nuevo:",
                reply_markup=obtener_menu_cancelar_flujo()
            )
            return

        if monto < 1000:
            await update.message.reply_text(
                "⚠️ El monto mínimo para simular es de $1.000. Ingresá un monto mayor:",
                reply_markup=obtener_menu_cancelar_flujo()
            )
            return

        precio = context.user_data["sim_precio"]
        ticker = context.user_data["sim_ticker"]
        tipo = context.user_data["sim_tipo"]
        comision = monto * COMISION
        cantidad = (monto - comision) / precio

        context.user_data["sim_monto"] = monto
        context.user_data["sim_cantidad"] = cantidad
        context.user_data["flujo_paso"] = "confirmar"

        resumen = (
            "📋 Paso 4 de 4: RESUMEN DE SIMULACIÓN\n\n"
            f"• Instrumento: {tipo}\n"
            f"• Activo: {nombre_corto(ticker)}\n"
            f"• Precio actual: ${formato_numero(precio)}\n"
            f"• Capital a invertir: ${formato_numero(monto)}\n"
            f"• Comisión estimada (0,5%): ${formato_numero(comision)}\n"
            f"• Cantidad estimada a comprar: {formato_numero(cantidad)} unidades\n\n"
            "¿Confirmás el registro de esta simulación?"
        )
        await update.message.reply_text(resumen, reply_markup=obtener_menu_confirmar_flujo())
        return

    # PASO 4: confirmación final
    if paso == "confirmar":
        if texto == BTN_CONFIRMAR_SIM:
            db.guardar_simulacion(
                update.effective_chat.id,
                context.user_data["sim_tipo"],
                context.user_data["sim_ticker"],
                context.user_data["sim_monto"],
                context.user_data["sim_precio"],
                context.user_data["sim_cantidad"],
            )
            print(
                f"\n[SIMULACIÓN GUARDADA EN DB] Chat: {update.effective_chat.id} | "
                f"Activo: {context.user_data['sim_ticker']} | "
                f"Monto: ${context.user_data['sim_monto']:,.2f}\n"
            )
            context.user_data.clear()
            await update.message.reply_text(
                "✅ ¡Simulación confirmada y guardada!\n"
                f"Podés ver cómo le va en {BTN_MIS_SIMULACIONES}.\n"
                "Recordá que es un cálculo orientativo y no una orden real de mercado.",
                reply_markup=obtener_menu()
            )
            return

        await update.message.reply_text(
            "Por favor, elegí '✅ Confirmar simulación' o '❌ Cancelar simulación'.",
            reply_markup=obtener_menu_confirmar_flujo()
        )


# ---------------------------------------------------------------------------
# COMANDOS (atajos con "/")
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    nombre = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola, {nombre}! 📈 Soy FinanzasBot.\n\n"
        "Consulto acciones argentinas y CEDEARs en pesos con datos de Yahoo Finance:\n"
        "• Tops del día de acciones y CEDEARs\n"
        "• Detalle y noticias de un activo\n"
        "• Simulador de inversión paso a paso\n"
        "• Chat con IA sobre finanzas (contame tu perfil de inversor)\n\n"
        "Elegí una opción del menú 👇",
        reply_markup=obtener_menu()
    )


async def cmd_top_acciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await mostrar_top(update, "acciones")


async def cmd_top_cedears(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await mostrar_top(update, "cedears")


async def _pedir_ticker(update, context, accion):
    """Deja al bot esperando un ticker para 'precio' o 'noticias'."""
    context.user_data.clear()
    context.user_data["esperando"] = accion
    que = "consultar" if accion == "precio" else "ver noticias"
    await update.message.reply_text(
        f"Escribí el símbolo del activo que querés {que}.\n\n"
        "Ejemplos: GGAL, YPFD, AAPL, NVDA",
        reply_markup=obtener_menu_volver()
    )


async def cmd_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await _pedir_ticker(update, context, "precio")
        return
    context.user_data.clear()
    ticker = finanzas.normalizar_ticker(context.args[0])
    if ticker is None:
        await update.message.reply_text("Ese símbolo no es válido. Ejemplo: /precio GGAL")
        return
    await buscar_activo(update, ticker)


async def cmd_noticias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await _pedir_ticker(update, context, "noticias")
        return
    context.user_data.clear()
    ticker = finanzas.normalizar_ticker(context.args[0])
    if ticker is None:
        await update.message.reply_text("Ese símbolo no es válido. Ejemplo: /noticias NVDA")
        return
    await mostrar_noticias(update, context, ticker)


async def cmd_simulaciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await mostrar_simulaciones(update)


async def borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cantidad = db.borrar(update.effective_chat.id)
    context.user_data.clear()
    await update.message.reply_text(
        f"🗑️ Conversación borrada ({cantidad} mensajes). Empecemos de nuevo.\n"
        "Tus simulaciones guardadas se mantienen.",
        reply_markup=obtener_menu()
    )


# ---------------------------------------------------------------------------
# RECIBIR MENSAJES (botones + chat libre)
# ---------------------------------------------------------------------------
async def mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    texto = update.message.text.strip()

    # Si el usuario está dentro del flujo guiado de simulación
    if context.user_data.get("flujo_paso"):
        await manejar_flujo_simulacion(update, context, texto)
        return

    # Botones del menú
    if texto == BTN_VOLVER:
        context.user_data.clear()
        await update.message.reply_text(
            "Volviste al menú principal. ¿Qué querés consultar?", reply_markup=obtener_menu()
        )
        return

    if texto == BTN_TOP_ACCIONES:
        context.user_data.clear()
        await mostrar_top(update, "acciones")
        return

    if texto == BTN_TOP_CEDEARS:
        context.user_data.clear()
        await mostrar_top(update, "cedears")
        return

    if texto == BTN_BUSCAR:
        await _pedir_ticker(update, context, "precio")
        return

    if texto == BTN_NOTICIAS:
        await _pedir_ticker(update, context, "noticias")
        return

    if texto == BTN_SIMULAR:
        context.user_data.clear()
        context.user_data["flujo_paso"] = "tipo"
        await update.message.reply_text(
            "🧮 SIMULADOR DE INVERSIÓN (Paso 1 de 4)\n\n"
            "Elegí qué tipo de instrumento querés simular:",
            reply_markup=obtener_menu_tipo_activo()
        )
        return

    if texto == BTN_MIS_SIMULACIONES:
        context.user_data.clear()
        await mostrar_simulaciones(update)
        return

    if texto == BTN_BORRAR:
        await borrar(update, context)
        return

    if texto == BTN_CHAT:
        context.user_data.clear()
        await update.message.reply_text(
            "Escribime tu consulta sobre finanzas y mercados. "
            "Si me contás tu perfil de inversor, te respondo a tu medida.",
            reply_markup=obtener_menu_volver()
        )
        return

    # Si el bot está esperando un ticker (después de Buscar activo o Noticias)
    esperando = context.user_data.get("esperando")
    if esperando:
        ticker = finanzas.normalizar_ticker(texto)
        if ticker is None:
            await update.message.reply_text(
                "⚠️ Eso no parece un símbolo. Escribí solo el ticker (ej. GGAL o NVDA) "
                f"o tocá {BTN_VOLVER}.",
                reply_markup=obtener_menu_volver()
            )
            return
        context.user_data.clear()
        if esperando == "precio":
            await buscar_activo(update, ticker)
        else:
            await mostrar_noticias(update, context, ticker)
        return

    # CONVERSACIÓN CON GEMINI (con datos de Yahoo + salida estructurada)
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    db.guardar(chat_id, "user", texto)
    historial = db.historial(chat_id, MAX_HISTORIAL)

    try:
        analisis = await asyncio.to_thread(preguntar_a_gemini_estructurado, historial, texto)
    except Exception:
        logger.exception("Error al llamar a Gemini")
        db.borrar_ultimo_mensaje(chat_id)  # no dejar la pregunta sin respuesta en el historial
        await update.message.reply_text(
            "⚠️ Tuve un problema para responder. Probá de nuevo en un rato.",
            reply_markup=obtener_menu_volver()
        )
        return

    respuesta = analisis.respuesta_usuario
    db.guardar_analisis(chat_id, texto, analisis.model_dump())
    db.guardar(chat_id, "model", respuesta)

    await responder(update, respuesta, obtener_menu_volver())


# ---------------------------------------------------------------------------
# INICIAR BOT
# ---------------------------------------------------------------------------
def main():
    db.init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("top_acciones", cmd_top_acciones))
    app.add_handler(CommandHandler("top_cedears", cmd_top_cedears))
    app.add_handler(CommandHandler("precio", cmd_precio))
    app.add_handler(CommandHandler("noticias", cmd_noticias))
    app.add_handler(CommandHandler("simulaciones", cmd_simulaciones))
    app.add_handler(CommandHandler("borrar", borrar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje))

    print(f"🤖 FinanzasBot en funcionamiento (modelo: {MODEL})...")
    app.run_polling()


if __name__ == "__main__":
    main()
