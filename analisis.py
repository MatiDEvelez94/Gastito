"""
Análisis mensual de gastos con salida estructurada (v5).

En lugar de pedirle al modelo un texto libre, se le exige una respuesta en
JSON que cumpla un esquema definido con Pydantic. Así el programa puede
guardar cada campo en su columna y comparar los análisis entre meses sin
interpretar texto.
"""

import json                              # convierte los gastos a texto para enviarlos al modelo
from typing import Literal               # restringe un campo a un conjunto fijo de valores

from pydantic import BaseModel, Field    # definición y validación de estructuras de datos

# Las mismas 8 categorías que usa el registro de gastos.
Categoria = Literal[
    "Supermercado", "Comida y delivery", "Transporte", "Hogar y servicios",
    "Estudio", "Salidas y ocio", "Salud", "Otros",
]


class AnalisisMes(BaseModel):
    """Estructura que debe tener el análisis de los gastos de un mes."""

    # Literal: el modelo solo puede responder uno de estos cuatro valores.
    estado: Literal["dentro del presupuesto", "en riesgo", "excedido", "sin presupuesto"] = Field(
        description="Situación del mes frente al presupuesto.",
    )
    categoria_principal: Categoria = Field(
        description="Categoría en la que más se gastó.",
    )
    # ge y le (greater or equal, less or equal) acotan el valor entre 0 y 100.
    porcentaje_principal: int = Field(
        ge=0, le=100,
        description="Porcentaje del total gastado que representa la categoría principal, redondeado.",
    )
    gastos_hormiga: list[str] = Field(
        description=(
            "Gastos chicos y repetidos que suman bastante (por ejemplo: 'café todos los días'). "
            "Lista vacía si no hay."
        ),
    )
    # max_length limita la lista a 3 elementos como máximo.
    recomendaciones: list[str] = Field(
        max_length=3,
        description="Hasta tres recomendaciones concretas y breves para el resto del mes.",
    )
    resumen: str = Field(
        description="Resumen del mes en una o dos oraciones, en español rioplatense.",
    )


# Instrucciones para esta tarea puntual. Son distintas del prompt de sistema
# de la conversación: aquí el modelo no conversa, solo analiza datos.
PROMPT_ANALISIS = """
Analizás los gastos mensuales de un estudiante argentino.
Recibís los gastos del mes, el total ya calculado y, si existe, su presupuesto.
Reglas para el estado:
- "sin presupuesto" si no hay presupuesto.
- "excedido" si el total gastado supera lo que tenía para gastar.
- "en riesgo" si ya gastó el 80 % o más de lo que tenía para gastar.
- "dentro del presupuesto" en cualquier otro caso.
Usá los números que recibís; no los inventes ni los recalcules de otra forma.
No des consejos de inversión. Respondé únicamente con el análisis pedido, en español.
""".strip()


async def analizar_mes(cliente, modelo: str, mes: str, gastos: list[dict], presupuesto: dict | None) -> AnalisisMes:
    """Envía los gastos del mes a Gemini y devuelve el análisis ya validado."""

    # Las cuentas las hace el código (es determinista y no se equivoca): el
    # modelo recibe el total y el porcentaje usado ya calculados.
    total = sum(gasto["monto_ars"] for gasto in gastos)
    datos = {"mes": mes, "total_gastado_ars": total, "gastos": gastos, "presupuesto": presupuesto}
    if presupuesto:
        datos["porcentaje_usado"] = round(total / presupuesto["para_gastar_ars"] * 100, 1)

    interaccion = await cliente.aio.interactions.create(
        model=modelo,
        system_instruction=PROMPT_ANALISIS,
        input=json.dumps(datos, ensure_ascii=False),
        # response_format exige una respuesta en JSON que respete el esquema.
        # model_json_schema() traduce la clase de Pydantic a JSON Schema.
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": AnalisisMes.model_json_schema(),
        },
        store=False,
    )

    # model_validate_json convierte el texto JSON en un objeto AnalisisMes y
    # verifica cada campo. Si el modelo devolviera un estado no permitido o
    # un porcentaje de 150, se produce un ValidationError.
    return AnalisisMes.model_validate_json(interaccion.output_text)