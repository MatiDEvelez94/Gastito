"""
Prueba aislada del análisis mensual: consulta a Gemini sin pasar por Telegram.
Usa gastos de ejemplo, así no depende de lo que haya en la base.
"""

import asyncio                           # permite ejecutar corrutinas desde un script
import os                                # acceso a las variables de entorno

from dotenv import load_dotenv           # lee el archivo .env
from google import genai                 # SDK oficial de Gemini

from analisis import analizar_mes        # la función que se quiere probar

load_dotenv()
cliente = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
modelo = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

GASTOS = [
    {"fecha": "2026-11-02", "categoria": "Supermercado", "descripcion": "súper", "monto_ars": 45000},
    {"fecha": "2026-11-03", "categoria": "Comida y delivery", "descripcion": "café", "monto_ars": 2500},
    {"fecha": "2026-11-04", "categoria": "Comida y delivery", "descripcion": "café", "monto_ars": 2500},
    {"fecha": "2026-11-05", "categoria": "Comida y delivery", "descripcion": "café", "monto_ars": 2500},
    {"fecha": "2026-11-06", "categoria": "Transporte", "descripcion": "SUBE", "monto_ars": 3000},
    {"fecha": "2026-11-08", "categoria": "Salidas y ocio", "descripcion": "Spotify", "monto_ars": 20475},
]
PRESUPUESTO = {"ingreso_ars": 200000, "meta_ahorro_ars": 30000, "para_gastar_ars": 170000}


async def main():
    # Caso 1: con presupuesto (debería dar "dentro del presupuesto").
    analisis = await analizar_mes(cliente, modelo, "2026-11", GASTOS, PRESUPUESTO)
    print(analisis.model_dump_json(indent=2))        # el objeto, mostrado como JSON

    # Caso 2: sin presupuesto (debería dar "sin presupuesto").
    analisis = await analizar_mes(cliente, modelo, "2026-11", GASTOS, None)
    print("\nSin presupuesto ->", analisis.estado, "|", analisis.categoria_principal)

asyncio.run(main())