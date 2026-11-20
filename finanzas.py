"""Datos de mercado desde Yahoo Finance (yfinance).

- Tops de acciones y CEDEARs, detalle de un activo y noticias.
- Contexto de mercado que se le pasa a Gemini antes de cada respuesta del chat.

Para que el bot responda rápido, los datos se descargan en lote (una sola
consulta a Yahoo para todos los tickers) y se guardan en caché unos minutos.
"""

import logging
import re
import threading
import time

import yfinance as yf

logger = logging.getLogger("FinanzasBot.finanzas")

# ---------------------------------------------------------------------------
# Activos monitoreados (en pesos, BYMA)
# ---------------------------------------------------------------------------
ACCIONES = [
    "GGAL.BA", "YPFD.BA", "PAMP.BA", "BMA.BA", "TXAR.BA",
    "ALUA.BA", "CEPU.BA", "EDN.BA", "TGSU2.BA", "TGNO4.BA",
    "COME.BA", "BBAR.BA", "SUPV.BA", "CRES.BA", "MIRG.BA",
    "LOMA.BA", "VALO.BA", "BYMA.BA", "TRAN.BA", "METR.BA",
]

CEDEARS = [
    "AAPL.BA", "MSFT.BA", "GOOGL.BA", "AMZN.BA", "TSLA.BA",
    "NVDA.BA", "META.BA", "MELI.BA", "KO.BA", "PEP.BA",
    "DIS.BA", "NFLX.BA", "AMD.BA", "INTC.BA", "BABA.BA",
    "NU.BA", "V.BA", "JPM.BA", "WMT.BA", "SPY.BA",
]

# Montos mínimos operados según la planilla del proyecto
MONTO_MIN_ACCIONES = 50_000
MONTO_MIN_CEDEARS = 1_000_000

# Cuánto tiempo (segundos) se reutilizan los datos antes de volver a pedirlos
CACHE_SEGUNDOS = 300

# Máximo de activos "extra" (no monitoreados) que se buscan por mensaje del chat
MAX_EXTRAS_POR_MENSAJE = 3

# Formato válido de un ticker: GGAL, YPFD, TGSU2, BRK-B, opcionalmente con .BA
PATRON_TICKER = re.compile(r"^[A-Z][A-Z0-9\-]{0,5}(\.BA)?$")

# ---------------------------------------------------------------------------
# Caché simple en memoria
# ---------------------------------------------------------------------------
_cache = {}
_lock = threading.Lock()


def _cache_get(clave):
    with _lock:
        item = _cache.get(clave)
    if item and time.time() - item[0] < CACHE_SEGUNDOS:
        return item[1]
    return None


def _cache_set(clave, valor):
    with _lock:
        _cache[clave] = (time.time(), valor)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def normalizar_ticker(texto):
    """'ggal' -> 'GGAL.BA'. Devuelve None si no tiene formato de ticker."""
    t = (texto or "").strip().upper().lstrip("$")
    if not PATRON_TICKER.match(t):
        return None
    return t if t.endswith(".BA") else f"{t}.BA"


def nombre_corto(ticker):
    return ticker.replace(".BA", "")


def formato_numero(numero, decimales=2):
    """1234567.891 -> '1.234.567,89' (formato argentino)."""
    texto = f"{numero:,.{decimales}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def formato_pct(numero):
    """3.4 -> '+3,40%'"""
    return f"{'+' if numero >= 0 else '-'}{formato_numero(abs(numero))}%"


# ---------------------------------------------------------------------------
# Cálculo de métricas a partir del histórico diario
# ---------------------------------------------------------------------------
def _calcular_periodo(datos_periodo, precio_actual):
    if len(datos_periodo) == 0:
        return 0.0, 0, 0.0
    primero = float(datos_periodo.iloc[0]["Close"])
    variacion = ((precio_actual - primero) / primero) * 100 if primero > 0 else 0.0
    volumen = int(datos_periodo["Volume"].sum())
    monto = float((datos_periodo["Close"] * datos_periodo["Volume"]).sum())
    return variacion, volumen, monto


def _metricas(ticker, datos):
    """Recibe el histórico diario (3 meses) de un ticker y arma el diccionario
    con día, semana, mes y 3 meses. Devuelve None si no hay datos suficientes."""
    datos = datos.dropna(subset=["Close"])
    if len(datos) < 2:
        return None

    ultimo = datos.iloc[-1]
    anterior = datos.iloc[-2]
    precio = float(ultimo["Close"])
    cierre_anterior = float(anterior["Close"])
    if cierre_anterior <= 0:
        return None

    volumen = int(ultimo["Volume"]) if ultimo["Volume"] == ultimo["Volume"] else 0
    var_sem, vol_sem, monto_sem = _calcular_periodo(datos.tail(5), precio)
    var_mes, vol_mes, monto_mes = _calcular_periodo(datos.tail(21), precio)
    var_3m, vol_3m, monto_3m = _calcular_periodo(datos, precio)

    return {
        "ticker": ticker,
        "precio": precio,
        "cierre_anterior": cierre_anterior,
        "variacion": ((precio - cierre_anterior) / cierre_anterior) * 100,
        "volumen": volumen,
        "monto_operado": precio * volumen,  # estimación en ARS
        "variacion_semanal": var_sem,
        "volumen_semanal": vol_sem,
        "monto_semanal": monto_sem,
        "variacion_mensual": var_mes,
        "volumen_mensual": vol_mes,
        "monto_mensual": monto_mes,
        "variacion_3m": var_3m,
        "volumen_3m": vol_3m,
        "monto_3m": monto_3m,
        "apertura": float(ultimo["Open"]),
        "minimo": float(ultimo["Low"]),
        "maximo": float(ultimo["High"]),
        "fecha": datos.index[-1].strftime("%d/%m/%Y"),
    }


# ---------------------------------------------------------------------------
# Descarga desde Yahoo Finance
# ---------------------------------------------------------------------------
def _descargar_lote(tickers):
    """Descarga 3 meses de todos los tickers en una sola consulta.
    Devuelve {ticker: métricas} solo con los que tienen datos."""
    clave = ("lote", tuple(tickers))
    en_cache = _cache_get(clave)
    if en_cache is not None:
        return en_cache

    logger.info("Descargando %d tickers de Yahoo Finance...", len(tickers))
    data = yf.download(
        tickers,
        period="3mo",
        interval="1d",
        group_by="ticker",
        progress=False,
        threads=True,
        auto_adjust=False,
    )

    resultado = {}
    for ticker in tickers:
        try:
            m = _metricas(ticker, data[ticker])
            if m:
                resultado[ticker] = m
        except Exception:
            continue

    _cache_set(clave, resultado)
    # También se guarda cada activo por separado para consultar_activo()
    for ticker, m in resultado.items():
        _cache_set(("activo", ticker), m)
    return resultado


def consultar_activo(ticker):
    """Detalle de un activo (ej. 'GGAL.BA'). Devuelve None si no existe o no hay datos."""
    clave = ("activo", ticker)
    en_cache = _cache_get(clave)
    if en_cache is not None:
        return en_cache or None  # {} = "ya sabemos que no existe"

    try:
        datos = yf.Ticker(ticker).history(period="3mo", interval="1d")
        m = _metricas(ticker, datos) if len(datos) else None
    except Exception as error:
        logger.warning("Error consultando %s: %s", ticker, error)
        m = None

    # Se cachea también el "no encontrado" para no volver a preguntarle a Yahoo
    _cache_set(clave, m or {})
    return m


def datos_monitoreados():
    """Métricas de todas las acciones y CEDEARs monitoreados."""
    return _descargar_lote(ACCIONES), _descargar_lote(CEDEARS)


# ---------------------------------------------------------------------------
# Tops (funciones 1 y 2)
# ---------------------------------------------------------------------------
def _top(datos, monto_minimo, cantidad=10):
    resultados = [
        d for d in datos.values()
        if d["variacion"] > 0 and d["monto_operado"] > monto_minimo
    ]
    resultados.sort(key=lambda d: d["variacion"], reverse=True)
    return resultados[:cantidad]


def obtener_top_acciones():
    return _top(_descargar_lote(ACCIONES), MONTO_MIN_ACCIONES)


def obtener_top_cedears():
    return _top(_descargar_lote(CEDEARS), MONTO_MIN_CEDEARS)


# ---------------------------------------------------------------------------
# Contexto de mercado para Gemini (requisito: Gemini trabaja con datos de Yahoo)
# ---------------------------------------------------------------------------
# Palabras comunes que tienen forma de ticker pero no lo son
_NO_TICKERS = {
    "HOLA", "QUE", "QUÉ", "LOS", "LAS", "DEL", "MAS", "MÁS", "SON", "CON", "POR",
    "UNA", "UNO", "COMO", "CÓMO", "PARA", "PERO", "ESTA", "ESTÁ", "ESTE", "ESO",
    "HOY", "AYER", "MES", "MESES", "SEMANA", "AÑO", "TRES", "TOP", "DIA", "DÍA",
    "CEDEAR", "CEDEARS", "ACCION", "ACCIÓN", "PRECIO", "VALOR", "DATO", "DATOS",
    "CUAL", "CUÁL", "CUANTO", "CUÁNTO", "DONDE", "SOBRE", "TENGO", "QUIERO",
    "SOY", "MIS", "TUS", "SUS", "MUY", "BIEN", "MAL", "SUBE", "BAJA", "BAJO",
    "ALTO", "DAME", "DECIME", "DIME", "PESOS", "DOLAR", "DÓLAR", "BOLSA",
    "ULTIMA", "ÚLTIMA", "ULTIMO", "ÚLTIMO", "GRACIAS", "TODO", "TODOS", "NADA",
    "RIESGO", "PERFIL", "CARTERA", "INVERTIR", "COMPRAR", "VENDER", "NOTICIA",
    "NOTICIAS", "ARS", "USD", "MEP", "CCL", "ETF", "BYMA", "BOT", "OK",
}


def _linea(d):
    return (
        f"- {nombre_corto(d['ticker'])}: Precio ${formato_numero(d['precio'])} | "
        f"Hoy: {formato_pct(d['variacion'])} (Monto ${formato_numero(d['monto_operado'], 0)}) | "
        f"Semana: {formato_pct(d['variacion_semanal'])} (Monto ${formato_numero(d['monto_semanal'], 0)}) | "
        f"Mes: {formato_pct(d['variacion_mensual'])} | "
        f"3 meses: {formato_pct(d['variacion_3m'])} | "
        f"Último dato: {d['fecha']}"
    )


def _tickers_en_texto(texto):
    """Busca en el mensaje activos que NO están en la lista monitoreada.

    Para no consultar a Yahoo por palabras comunes ("hay", "porque"...), solo se
    toman como ticker las palabras escritas en MAYÚSCULAS (XOM), con $ adelante
    ($xom) o con .BA (xom.ba). Los activos monitoreados ya van siempre en el contexto.
    """
    palabras = re.findall(r"\$?\b[A-Za-z][A-Za-z0-9]{1,5}(?:\.[Bb][Aa])?\b", texto or "")
    candidatos = []
    for p in palabras:
        explicito = p.startswith("$") or p.upper().endswith(".BA") or p.isupper()
        if not explicito or p.upper().lstrip("$") in _NO_TICKERS:
            continue
        ticker = normalizar_ticker(p)
        if ticker and ticker not in candidatos:
            candidatos.append(ticker)
    return candidatos


def obtener_contexto_mercado(consulta_usuario=""):
    """Arma el bloque de datos reales de Yahoo Finance que se le pasa a Gemini."""
    lineas = ["[DATOS DE MERCADO EN TIEMPO REAL - YAHOO FINANCE (BYMA, en pesos)]"]

    try:
        acciones, cedears = datos_monitoreados()
    except Exception:
        logger.exception("No se pudieron descargar los datos monitoreados")
        acciones, cedears = {}, {}

    if cedears:
        lineas.append("\nCEDEARs (ordenados por monto operado en la semana):")
        for d in sorted(cedears.values(), key=lambda x: x["monto_semanal"], reverse=True):
            lineas.append(_linea(d))

    if acciones:
        lineas.append("\nAcciones argentinas (ordenadas por monto operado en la semana):")
        for d in sorted(acciones.values(), key=lambda x: x["monto_semanal"], reverse=True):
            lineas.append(_linea(d))

    if not acciones and not cedears:
        lineas.append("\n(No se pudieron obtener datos de Yahoo Finance en este momento.)")

    # Activos que el usuario menciona y no están en la lista monitoreada
    conocidos = set(acciones) | set(cedears)
    extras = 0
    for ticker in _tickers_en_texto(consulta_usuario):
        if ticker in conocidos or extras >= MAX_EXTRAS_POR_MENSAJE:
            continue
        extras += 1
        d = consultar_activo(ticker)
        if d:
            lineas.append(f"\nActivo adicional consultado:\n{_linea(d)}")

    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Noticias
# ---------------------------------------------------------------------------
# Las acciones argentinas tienen noticias bajo el ticker de su ADR en EE.UU.
TICKER_NOTICIAS = {
    "YPFD": "YPF", "PAMP": "PAM", "TGSU2": "TGS", "CRES": "CRESY",
    "TECO2": "TEO", "IRSA": "IRS",
}


def obtener_noticias(simbolo, cantidad=5):
    """Devuelve (ticker_usado, [noticias]) con título, resumen, fuente y url."""
    simbolo = nombre_corto(simbolo.strip().upper())
    simbolo = TICKER_NOTICIAS.get(simbolo, simbolo)

    noticias = []
    for item in (yf.Ticker(simbolo).news or [])[:cantidad]:
        c = item.get("content", item)  # yfinance cambió el formato; soporta ambos
        url = (c.get("canonicalUrl") or c.get("clickThroughUrl") or {}).get("url") or c.get("link")
        fuente = (c.get("provider") or {}).get("displayName") or c.get("publisher", "")
        noticias.append({
            "titulo": c.get("title", "(sin título)"),
            "resumen": c.get("summary", ""),
            "fuente": fuente,
            "url": url,
        })
    return simbolo, noticias
