"""Base de datos SQLite: historial de conversación, análisis estructurado y simulaciones."""

import json
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "historial.db")


def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                rol TEXT NOT NULL,
                contenido TEXT NOT NULL,
                creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_mensajes_chat ON mensajes (chat_id)"
        )

        c.execute(
            """CREATE TABLE IF NOT EXISTS analisis_estructurado (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                consulta TEXT NOT NULL,
                perfil_detectado TEXT NOT NULL,
                intencion TEXT NOT NULL,
                activos_mencionados TEXT NOT NULL,
                nivel_riesgo TEXT NOT NULL,
                datos_json TEXT NOT NULL,
                creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )

        c.execute(
            """CREATE TABLE IF NOT EXISTS simulaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                tipo_activo TEXT NOT NULL,
                ticker TEXT NOT NULL,
                monto_pesos REAL NOT NULL,
                precio_unitario REAL NOT NULL,
                cantidad_estimada REAL NOT NULL,
                creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )


# ---------------------------------------------------------------------------
# Historial de conversación con Gemini
# ---------------------------------------------------------------------------
def guardar(chat_id, rol, contenido):
    with _conn() as c:
        c.execute(
            "INSERT INTO mensajes (chat_id, rol, contenido) VALUES (?, ?, ?)",
            (chat_id, rol, contenido),
        )


def historial(chat_id, limite=40):
    """Últimos `limite` mensajes del chat, del más viejo al más nuevo:
    [{"role": "user"/"model", "content": texto}]"""
    with _conn() as c:
        filas = c.execute(
            """SELECT rol, contenido FROM (
                   SELECT id, rol, contenido FROM mensajes
                   WHERE chat_id = ? ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (chat_id, limite),
        ).fetchall()

    mensajes = [{"role": rol, "content": texto} for rol, texto in filas]

    # La conversación que se manda a Gemini tiene que empezar con el usuario
    while mensajes and mensajes[0]["role"] != "user":
        mensajes.pop(0)
    return mensajes


def borrar_ultimo_mensaje(chat_id):
    """Se usa cuando Gemini falla, para no dejar una pregunta sin respuesta en el historial."""
    with _conn() as c:
        c.execute(
            "DELETE FROM mensajes WHERE id = (SELECT MAX(id) FROM mensajes WHERE chat_id = ?)",
            (chat_id,),
        )


def borrar(chat_id):
    """Borra la conversación del chat. Devuelve cuántos mensajes se borraron.
    (Las simulaciones guardadas NO se borran.)"""
    with _conn() as c:
        cursor = c.execute("DELETE FROM mensajes WHERE chat_id = ?", (chat_id,))
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Salida estructurada
# ---------------------------------------------------------------------------
def guardar_analisis(chat_id, consulta, analisis_dict):
    with _conn() as c:
        c.execute(
            """INSERT INTO analisis_estructurado
               (chat_id, consulta, perfil_detectado, intencion, activos_mencionados, nivel_riesgo, datos_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                chat_id,
                consulta,
                analisis_dict.get("perfil_detectado", "no_especificado"),
                analisis_dict.get("intencion", "educacion_financiera"),
                ", ".join(analisis_dict.get("activos_mencionados", [])),
                analisis_dict.get("nivel_riesgo", "no_aplica"),
                json.dumps(analisis_dict, ensure_ascii=False),
            ),
        )


# ---------------------------------------------------------------------------
# Simulaciones (flujo guiado)
# ---------------------------------------------------------------------------
def guardar_simulacion(chat_id, tipo_activo, ticker, monto_pesos, precio_unitario, cantidad_estimada):
    with _conn() as c:
        c.execute(
            """INSERT INTO simulaciones
               (chat_id, tipo_activo, ticker, monto_pesos, precio_unitario, cantidad_estimada)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, tipo_activo, ticker, monto_pesos, precio_unitario, cantidad_estimada),
        )


def listar_simulaciones(chat_id, limite=10):
    """Últimas simulaciones del chat, de la más nueva a la más vieja."""
    with _conn() as c:
        filas = c.execute(
            """SELECT tipo_activo, ticker, monto_pesos, precio_unitario, cantidad_estimada,
                      strftime('%d/%m/%Y', creado, 'localtime')
               FROM simulaciones WHERE chat_id = ?
               ORDER BY id DESC LIMIT ?""",
            (chat_id, limite),
        ).fetchall()

    return [
        {
            "tipo": tipo,
            "ticker": ticker,
            "monto": monto,
            "precio_compra": precio,
            "cantidad": cantidad,
            "fecha": fecha,
        }
        for tipo, ticker, monto, precio, cantidad, fecha in filas
    ]
