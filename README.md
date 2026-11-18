# FinanzasBot 📈

Bot de Telegram para consultar acciones argentinas y CEDEARs en pesos (ARS) vía Yahoo Finance,
con un asistente conversacional basado en Gemini (Interactions API, sin estado) y memoria en SQLite.

**Integrantes:** Facundo Benegas, Ezequiel Ramos

## Comandos

| Comando | Qué hace |
|---|---|
| `/start` | Bienvenida y ayuda |
| `/top_acciones` | Top 10 acciones con mayor suba diaria (monto > $50.000) |
| `/top_cedears` | Top 10 CEDEARs con mayor suba diaria (monto > $1.000.000) |
| `/precio TICKER` | Detalle de un activo (ej: `/precio GGAL`) |
| `/noticias TICKER` | Últimas noticias de una empresa, resumidas en español por Gemini (ej: `/noticias NVDA`) |
| `/borrar` | Borra el historial de la conversación con la IA |

Cualquier mensaje que **no** sea un comando se responde con Gemini, usando el historial guardado en SQLite.

### Sobre `/noticias`

- Las noticias salen de Yahoo Finance (vía `yfinance`) y Gemini las resume en español, sin agregar datos que no estén en las noticias. Al final se muestran los links a las fuentes.
- El resumen se guarda en el historial, así después se puede seguir charlando sobre esas noticias con la IA.
- Funciona con CEDEARs (empresas de EE.UU.) y con acciones argentinas que tienen ADR en Wall Street. Para las que tienen un ticker distinto en EE.UU., el bot lo traduce con el diccionario `TICKER_NOTICIAS` (por ejemplo, `YPFD` → `YPF`, `PAMP` → `PAM`).
- Las acciones que solo cotizan en BYMA (por ejemplo, TXAR o ALUA) pueden no tener noticias en Yahoo.

## Estructura

```
bot.py           # Handlers de Telegram, Yahoo Finance (precios y noticias) y llamada a Gemini
prompts.py       # Prompt de sistema (identidad, alcance, estilo, formato, honestidad)
database.py      # Historial de conversación en SQLite
.env.example     # Variables de entorno necesarias (copiar como .env)
requirements.txt
```

## Cómo correrlo

```bash
python -m venv venv
# Windows: venv\Scripts\activate   |   Linux/Mac: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # en Windows: copy .env.example .env
# completar TELEGRAM_TOKEN, GEMINI_API_KEY y GEMINI_MODEL en .env

python bot.py
```

La base `finanzasbot.db` se crea sola la primera vez. Ni `.env` ni `*.db` se suben al repositorio (ver `.gitignore`).
