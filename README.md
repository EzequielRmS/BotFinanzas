# FinanzasBot 📈

Bot de Telegram para consultar acciones argentinas y CEDEARs en pesos (ARS) con datos de
Yahoo Finance, con un asistente conversacional basado en Gemini (Interactions API, sin estado)
que responde usando esos datos reales. El historial se guarda en SQLite.

**Integrantes:** Facundo Benegas, Ezequiel Ramos

## Menú

El bot se usa con botones (aparecen al enviar `/start`):

| Botón | Qué hace |
|---|---|
| 📈 Top acciones | Top 10 acciones con mayor suba diaria (monto operado > $50.000) |
| 🌎 Top CEDEARs | Top 10 CEDEARs con mayor suba diaria (monto operado > $1.000.000) |
| 🔎 Buscar activo | Pide un ticker y muestra precio, variación diaria/semanal/mensual/3 meses, apertura, mínimo, máximo y monto operado |
| 📰 Noticias | Pide un ticker y muestra sus últimas noticias resumidas en español por Gemini, con links a las fuentes |
| 🧮 Simular inversión | Flujo guiado de 4 pasos: tipo de activo → ticker → monto → confirmación |
| 📂 Mis simulaciones | Muestra las simulaciones guardadas y cuánto valen hoy (ganancia/pérdida) |
| 💬 Consultar a FinanzasBot | Chat libre con la IA sobre finanzas e inversiones |
| 🗑️ Borrar conversación | Borra el historial de la conversación con la IA |

Cualquier mensaje que no sea un botón se responde con Gemini.

También funcionan estos comandos como atajo: `/start`, `/top_acciones`, `/top_cedears`,
`/precio TICKER`, `/noticias TICKER`, `/simulaciones` y `/borrar`.

## Cómo usa Gemini los datos de Yahoo Finance

Antes de cada respuesta del chat, el bot descarga los datos de los 40 activos monitoreados
(día, semana, mes y 3 meses) y se los pasa a Gemini junto con el prompt de sistema.
Si el usuario menciona un activo que no está en la lista (escrito en mayúsculas o con `$`,
por ejemplo `XOM` o `$xom`), también se busca y se agrega.

Para que responda rápido, los datos se piden a Yahoo en una sola consulta y se reutilizan
durante 5 minutos (`CACHE_SEGUNDOS` en `finanzas.py`).

Gemini devuelve una **salida estructurada** (validada con Pydantic) con la respuesta, el perfil
de inversor detectado, la intención de la consulta, los activos mencionados y el nivel de riesgo.
Se imprime en la terminal y se guarda en la tabla `analisis_estructurado`.

### Sobre las noticias

- Funcionan con CEDEARs y con acciones argentinas que tienen ADR en Wall Street. Para las que
  tienen otro ticker en EE.UU., el bot lo traduce con `TICKER_NOTICIAS` (por ejemplo,
  `YPFD` → `YPF`, `PAMP` → `PAM`).
- Las acciones que solo cotizan en BYMA (por ejemplo, TXAR o ALUA) pueden no tener noticias.
- El resumen se guarda en el historial, así después se puede seguir charlando sobre esas noticias.

## Módulos del hito 4

- **Flujo guiado:** el simulador de inversión. Valida cada dato (opción, formato del ticker,
  que el activo exista, monto numérico y mínimo de $1.000), se puede cancelar en cualquier paso
  y pide confirmación antes de guardar.
- **Evaluación:** `evaluar.py` corre 8 casos propios, muestra un resumen con porcentajes
  (general y por criterio) y genera `reporte_evaluacion.txt` con el análisis de cada caso fallido.

```bash
python evaluar.py
```

## Estructura

```
bot.py           # Menú, comandos, simulador, chat con Gemini y salida estructurada
finanzas.py      # Datos de Yahoo Finance: tops, detalle, noticias y contexto para Gemini
prompts.py       # Prompt de sistema (identidad, alcance, estilo, formato, honestidad)
db.py            # SQLite: historial, análisis estructurado y simulaciones
evaluar.py       # Evaluación automatizada (8 casos)
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

La base `historial.db` se crea sola la primera vez. Ni `.env` ni `*.db` se suben al
repositorio (ver `.gitignore`).

> Las respuestas del bot son orientativas y no constituyen asesoramiento financiero.
