"""Prueba aislada del módulo db.py, antes de conectarlo con el bot."""

import asyncio                          # permite ejecutar corrutinas desde un script

import db                               # el módulo de la base de datos


async def main():
    await db.inicializar()                                                    # crea la tabla
    await db.guardar_intercambio(1, "Gasté 8500 en el súper", "Anotado: $8.500 en Supermercado.")  # usuario de prueba: id 1
    await db.guardar_intercambio(1, "¿Cuánto llevo?", "Llevás $8.500 en total.")
    print(await db.obtener_historial(1, 20))                                  # los cuatro mensajes, en orden
    print(await db.obtener_historial(1, 2))                                   # solo los dos últimos
    print("Borrados:", await db.borrar_historial(1))                          # cantidad eliminada
    print(await db.obtener_historial(1, 20))                                  # lista vacía

asyncio.run(main())                     # pone en marcha el bucle de eventos