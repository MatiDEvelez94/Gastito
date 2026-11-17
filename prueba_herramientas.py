"""
Prueba aislada de las herramientas, sin Telegram ni Gemini.
Llama a cada función directamente y muestra lo que devuelve.
"""

import asyncio                          # permite ejecutar corrutinas desde un script
import json                             # para mostrar los resultados con formato legible

import db
from herramientas import ejecutar_funcion

USUARIO_PRUEBA = 1                      # id ficticio: no se mezcla con usuarios reales


def mostrar(titulo, resultado):
    print(f"\n--- {titulo}")
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


async def main():
    await db.inicializar()              # crea la tabla gastos si no existe

    # Casos normales.
    mostrar("Registrar gasto", await ejecutar_funcion(
        "registrar_gasto", {"monto": 8500, "categoria": "Supermercado", "descripcion": "súper"}, USUARIO_PRUEBA))
    mostrar("Consultar gastos del mes", await ejecutar_funcion("consultar_gastos", {}, USUARIO_PRUEBA))
    mostrar("Inflación de los últimos 3 meses (API real)", await ejecutar_funcion(
        "consultar_inflacion", {"meses": 3}, USUARIO_PRUEBA))
    mostrar("Dólar tarjeta (API real)", await ejecutar_funcion(
        "obtener_cotizacion_dolar", {"tipo": "tarjeta"}, USUARIO_PRUEBA))

    # Casos de error: deben devolver un diccionario con la clave "error", no fallar.
    mostrar("Monto negativo", await ejecutar_funcion(
        "registrar_gasto", {"monto": -50, "categoria": "Otros"}, USUARIO_PRUEBA))
    mostrar("Categoría inexistente", await ejecutar_funcion(
        "registrar_gasto", {"monto": 100, "categoria": "Viajes"}, USUARIO_PRUEBA))
    mostrar("Dólar euro", await ejecutar_funcion("obtener_cotizacion_dolar", {"tipo": "euro"}, USUARIO_PRUEBA))
    mostrar("Función inexistente", await ejecutar_funcion("funcion_inexistente", {}, USUARIO_PRUEBA))

    # Limpieza: se borran los gastos del usuario de prueba.
    await db.borrar_gastos(USUARIO_PRUEBA)
    print("\nGastos de prueba borrados.")

asyncio.run(main())