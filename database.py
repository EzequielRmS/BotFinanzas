"""Historial de conversación en SQLite (un historial por chat de Telegram)."""

import sqlite3
from datetime import datetime

DB_PATH = "finanzasbot.db"


def _conectar():
    return sqlite3.connect(DB_PATH)


def inicializar_db():
    with _conectar() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mensajes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER NOT NULL,
                rol       TEXT    NOT NULL CHECK (rol IN ('user', 'model')),
                contenido TEXT    NOT NULL,
                fecha     TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mensajes_chat ON mensajes (chat_id)"
        )


def guardar_mensaje(chat_id: int, rol: str, contenido: str):
    with _conectar() as conn:
        conn.execute(
            "INSERT INTO mensajes (chat_id, rol, contenido, fecha) VALUES (?, ?, ?, ?)",
            (chat_id, rol, contenido, datetime.now().isoformat(timespec="seconds")),
        )


def obtener_historial(chat_id: int, limite: int = 40):
    """Devuelve los últimos `limite` mensajes del chat, del más viejo al más nuevo,
    en el formato que espera la Interactions API: [{"role": ..., "content": ...}]."""
    with _conectar() as conn:
        filas = conn.execute(
            """
            SELECT rol, contenido FROM (
                SELECT id, rol, contenido FROM mensajes
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, limite),
        ).fetchall()

    historial = [{"role": rol, "content": contenido} for rol, contenido in filas]

    # La conversación tiene que empezar con un mensaje del usuario
    while historial and historial[0]["role"] != "user":
        historial.pop(0)
    return historial


def borrar_ultimo_mensaje(chat_id: int):
    with _conectar() as conn:
        conn.execute(
            """
            DELETE FROM mensajes WHERE id = (
                SELECT MAX(id) FROM mensajes WHERE chat_id = ?
            )
            """,
            (chat_id,),
        )


def borrar_historial(chat_id: int) -> int:
    with _conectar() as conn:
        cursor = conn.execute("DELETE FROM mensajes WHERE chat_id = ?", (chat_id,))
        return cursor.rowcount
