"""Evaluación automatizada de FinanzasBot (módulo Evaluación del hito 4).

Corre 8 casos propios contra Gemini (con los datos reales de Yahoo Finance),
muestra un resumen con porcentajes y genera reporte_evaluacion.txt.
Para cada caso que falla, el reporte explica qué criterio no se cumplió y qué
cambiaríamos.

Uso:  python evaluar.py
"""

import time
from datetime import datetime

from bot import preguntar_a_gemini_estructurado

ARCHIVO_REPORTE = "reporte_evaluacion.txt"

# Cada caso puede chequear:
# - esperado_intencion: la intención que debería detectar el esquema Pydantic
# - esperado_perfil:    el perfil de inversor que debería detectar
# - palabras_clave:     al menos una debe aparecer en la respuesta
# - mejora:             qué cambiaríamos si este caso falla
CASOS_PRUEBA = [
    {
        "id": 1,
        "nombre": "Ranking de CEDEARs semanales con datos de Yahoo Finance",
        "historial": [{"role": "user", "content": "¿Cuáles son los 3 CEDEARs más operados de la última semana?"}],
        "esperado_intencion": "ranking",
        "palabras_clave": ["$", "%"],
        "mejora": "Reforzar en el prompt que los rankings se responden con el bloque de datos de Yahoo Finance.",
    },
    {
        "id": 2,
        "nombre": "Detección de perfil conservador",
        "historial": [{"role": "user", "content": "Soy un inversor conservador, tengo $500.000 y quiero cuidar mi capital con bajo riesgo."}],
        "esperado_perfil": "conservador",
        "palabras_clave": ["riesgo", "conservador"],
        "mejora": "Agregar al prompt ejemplos de frases que indican cada perfil de inversor.",
    },
    {
        "id": 3,
        "nombre": "Rechazo de tema ajeno a finanzas (receta de cocina)",
        "historial": [{"role": "user", "content": "¿Cómo preparo un pastel de papas casero?"}],
        "esperado_intencion": "fuera_de_tema",
        "palabras_clave": ["finanzas", "mercado", "inversi"],
        "mejora": "Hacer más explícita en el prompt la regla de redirigir los temas ajenos.",
    },
    {
        "id": 4,
        "nombre": "Consulta de cotización puntual de acción argentina (GGAL)",
        "historial": [{"role": "user", "content": "¿Cuál es el precio actual y variación de GGAL?"}],
        "esperado_intencion": "cotizacion",
        "palabras_clave": ["GGAL"],
        "mejora": "Verificar que Yahoo Finance devolvió datos de GGAL y que el prompt obliga a citarlos.",
    },
    {
        "id": 5,
        "nombre": "Concepto educativo (ratio Beta)",
        "historial": [{"role": "user", "content": "Explicame brevemente qué es el ratio Beta de una acción."}],
        "esperado_intencion": "educacion_financiera",
        "palabras_clave": ["volatilidad", "mercado"],
        "mejora": "Pedir en el prompt que las explicaciones incluyan un ejemplo con un activo del panel.",
    },
    {
        "id": 6,
        "nombre": "Memoria de conversación en múltiples turnos",
        "historial": [
            {"role": "user", "content": "Soy un inversor agresivo y sigo a NVDA."},
            {"role": "model", "content": "Registrado: tienes un perfil agresivo y sigues el CEDEAR NVDA."},
            {"role": "user", "content": "¿Qué activo te dije que sigo y cuál es mi perfil?"},
        ],
        "esperado_perfil": "agresivo",
        "palabras_clave": ["NVDA"],
        "mejora": "Revisar que el historial completo llegue a Gemini y reforzar en el prompt el uso de datos del usuario.",
    },
    {
        "id": 7,
        "nombre": "No dar recomendación de compra garantizada",
        "historial": [{"role": "user", "content": "Decime qué acción comprar hoy para duplicar mi plata seguro mañana."}],
        "palabras_clave": ["asesoramiento", "riesgo", "garantiz"],
        "mejora": "Agregar al prompt una respuesta modelo para pedidos de ganancias aseguradas.",
    },
    {
        "id": 8,
        "nombre": "Cálculo de impuestos personales (caso diseñado para mostrar un límite del esquema)",
        "historial": [{"role": "user", "content": "Calculame el monto exacto en pesos que debo pagarle a ARCA por Bienes Personales este año."}],
        "esperado_intencion": "impuestos_personales",  # no existe en el esquema: muestra su límite
        "palabras_clave": ["contador"],
        "mejora": (
            "El esquema Pydantic no tiene una categoría para impuestos. Agregaríamos "
            "'normativa_e_impuestos' al Literal de 'intencion' y una regla en el prompt "
            "para derivar las consultas tributarias a un contador público."
        ),
    },
]


def evaluar_caso(caso):
    ultima = caso["historial"][-1]["content"]
    analisis = preguntar_a_gemini_estructurado(caso["historial"], ultima)
    respuesta = analisis.respuesta_usuario
    resp_lower = respuesta.lower()

    chequeos = {}  # criterio -> (cumple, detalle si falla)

    if "esperado_intencion" in caso:
        ok = analisis.intencion == caso["esperado_intencion"]
        chequeos["intencion"] = (
            ok, f"se esperaba intención '{caso['esperado_intencion']}' y se obtuvo '{analisis.intencion}'"
        )

    if "esperado_perfil" in caso:
        ok = analisis.perfil_detectado == caso["esperado_perfil"]
        chequeos["perfil"] = (
            ok, f"se esperaba perfil '{caso['esperado_perfil']}' y se obtuvo '{analisis.perfil_detectado}'"
        )

    ok = any(p.lower() in resp_lower for p in caso["palabras_clave"])
    chequeos["palabras_clave"] = (
        ok, f"la respuesta no contiene ninguna de: {', '.join(caso['palabras_clave'])}"
    )

    return {
        "intencion": analisis.intencion,
        "perfil": analisis.perfil_detectado,
        "respuesta": respuesta,
        "chequeos": chequeos,
    }


def ejecutar_evaluacion():
    total = len(CASOS_PRUEBA)
    print(f"Iniciando evaluación automatizada de FinanzasBot ({total} casos)...\n")
    resultados = []

    for caso in CASOS_PRUEBA:
        print(f"Ejecutando caso #{caso['id']}: {caso['nombre']}...")
        try:
            r = evaluar_caso(caso)
            r["error"] = None
        except Exception as e:
            r = {"intencion": "ERROR", "perfil": "ERROR", "respuesta": "", "chequeos": {}, "error": str(e)}

        r["exito"] = r["error"] is None and all(ok for ok, _ in r["chequeos"].values())
        r["caso"] = caso
        resultados.append(r)
        print(f"  -> {'APROBADO' if r['exito'] else 'FALLIDO'}")
        time.sleep(2)  # para no pasarse del límite de pedidos de la API gratuita

    # ---------------- Resumen con porcentajes ----------------
    aprobados = sum(r["exito"] for r in resultados)

    def pct_criterio(criterio):
        evaluados = [r for r in resultados if criterio in r["chequeos"]]
        if not evaluados:
            return "sin casos"
        ok = sum(r["chequeos"][criterio][0] for r in evaluados)
        return f"{ok}/{len(evaluados)} ({ok / len(evaluados) * 100:.1f}%)"

    lineas = [
        "============================================================",
        "          REPORTE DE EVALUACIÓN - FINANZASBOT (HITO 4)",
        f"          {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "============================================================",
        f"Total de casos evaluados : {total}",
        f"Casos aprobados          : {aprobados}",
        f"Casos fallidos           : {total - aprobados}",
        f"Porcentaje de efectividad: {aprobados / total * 100:.1f}%",
        "",
        "Aciertos por criterio:",
        f"  - Intención detectada  : {pct_criterio('intencion')}",
        f"  - Perfil detectado     : {pct_criterio('perfil')}",
        f"  - Palabras clave       : {pct_criterio('palabras_clave')}",
        "============================================================\n",
    ]

    for r in resultados:
        caso = r["caso"]
        lineas.append(f"Caso #{caso['id']}: {caso['nombre']} -> [{'APROBADO' if r['exito'] else 'FALLIDO'}]")
        lineas.append(f"  - Pregunta           : {caso['historial'][-1]['content']}")
        lineas.append(f"  - Intención detectada: {r['intencion']}")
        lineas.append(f"  - Perfil detectado   : {r['perfil']}")
        lineas.append(f"  - Respuesta del bot  : {r['respuesta'] or '(sin respuesta)'}\n")

    # ---------------- Análisis de los casos fallidos ----------------
    fallidos = [r for r in resultados if not r["exito"]]
    lineas.append("============================================================")
    lineas.append("ANÁLISIS DE LOS CASOS FALLIDOS")
    lineas.append("============================================================")
    if not fallidos:
        lineas.append("No hubo casos fallidos en esta ejecución.")
    for r in fallidos:
        caso = r["caso"]
        lineas.append(f"\nCaso #{caso['id']}: {caso['nombre']}")
        if r["error"]:
            lineas.append(f"- Por qué falló: error al ejecutar el caso ({r['error']}).")
            lineas.append("- Qué cambiaríamos: revisar la conexión con Gemini/Yahoo y volver a correrlo.")
        else:
            motivos = [detalle for ok, detalle in r["chequeos"].values() if not ok]
            lineas.append("- Por qué falló: " + "; ".join(motivos) + ".")
            lineas.append(f"- Qué cambiaríamos: {caso['mejora']}")

    texto_final = "\n".join(lineas)
    with open(ARCHIVO_REPORTE, "w", encoding="utf-8") as f:
        f.write(texto_final)

    print("\n" + texto_final)
    print(f"\nReporte guardado en '{ARCHIVO_REPORTE}'.")


if __name__ == "__main__":
    ejecutar_evaluacion()
