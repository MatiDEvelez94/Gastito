"""
Herramientas de Gastito: funciones que el modelo puede pedir que se ejecuten.

El módulo tiene tres partes:
   1. Las funciones propiamente dichas: dos guardan y leen gastos en la base,
      y dos consultan APIs externas (ArgentinaDatos y DolarAPI).
   2. Las declaraciones: la descripción de cada función en JSON Schema,
      que es lo único que el modelo ve de ellas.
   3. Un despachador que ejecuta la función pedida por el modelo.
"""

import logging                           # registro de eventos en la terminal
import unicodedata                       # para quitar tildes al comparar nombres
from datetime import date, datetime, timedelta, timezone

import httpx                             # cliente HTTP asíncrono (dependencia de python-telegram-bot)

import db                                # módulo propio: acceso a la base SQLite

logger = logging.getLogger("gastito")    # mismo registrador que bot.py

# Tiempo máximo de espera de cada consulta HTTP, en segundos. Sin un límite,
# una API que no responde dejaría al usuario esperando indefinidamente.
TIEMPO_ESPERA = 10

# ---------------------------------------------------------------------------
# 1. Datos fijos
# ---------------------------------------------------------------------------

# Categorías de gasto que maneja Gastito. Las mismas que muestra /categorias.
CATEGORIAS = [
    "Supermercado",
    "Comida y delivery",
    "Transporte",
    "Hogar y servicios",
    "Estudio",
    "Salidas y ocio",
    "Salud",
    "Otros",
]

# Tipos de cotización que acepta DolarAPI y que Gastito ofrece.
TIPOS_DOLAR = ["oficial", "blue", "tarjeta", "bolsa"]

# Hora de Argentina (UTC-3, sin horario de verano). Se usa un desfase fijo
# para no depender de la base de zonas horarias, que en Windows no viene instalada.
ZONA_ARGENTINA = timezone(timedelta(hours=-3))


def hoy() -> date:
    """Fecha de hoy en Argentina."""
    return datetime.now(ZONA_ARGENTINA).date()


def _normalizar(texto: str) -> str:
    """Pasa a minúsculas y quita tildes: 'Estudio' y 'estúdio' quedan iguales."""
    sin_tildes = unicodedata.normalize("NFD", texto)
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").lower().strip()


# ---------------------------------------------------------------------------
# 2. Funciones
# ---------------------------------------------------------------------------

async def registrar_gasto(user_id: int, monto: float, categoria: str,
                          descripcion: str = "", fecha: str | None = None) -> dict:
    """Guarda un gasto del usuario en la base de datos."""

    # El modelo podría pedir un monto negativo, cero o absurdo: se valida.
    monto = float(monto)
    if monto <= 0 or monto > 100_000_000:
        return {"error": f"Monto inválido: {monto}. Debe ser mayor que cero."}

    # Se busca la categoría en la lista, comparando sin tildes ni mayúsculas.
    coincidencias = [c for c in CATEGORIAS if _normalizar(c) == _normalizar(categoria)]
    if not coincidencias:
        # Se devuelve el error como dato, no como excepción: así el modelo
        # puede explicárselo al usuario y elegir una categoría válida.
        return {"error": f"Categoría desconocida: {categoria}", "categorias_disponibles": CATEGORIAS}
    categoria = coincidencias[0]

    # Si el usuario no dijo la fecha, el gasto es de hoy.
    if not fecha:
        dia = hoy()
    else:
        try:
            dia = date.fromisoformat(fecha)          # espera el formato AAAA-MM-DD
        except ValueError:
            return {"error": f"Fecha inválida: {fecha}. Usá el formato AAAA-MM-DD."}
        if dia > hoy():
            return {"error": "La fecha no puede ser futura: solo se registran gastos ya hechos."}

    gasto_id = await db.guardar_gasto(user_id, monto, categoria, descripcion.strip(), dia.isoformat())
    return {"registrado": True, "id": gasto_id, "monto_ars": monto,
            "categoria": categoria, "descripcion": descripcion, "fecha": dia.isoformat()}


async def consultar_gastos(user_id: int, mes: str | None = None) -> dict:
    """Devuelve el total de gastos de un mes, por categoría, leído de la base."""

    # Si no se indica el mes, se usa el actual. Formato AAAA-MM.
    mes = mes or hoy().strftime("%Y-%m")
    try:
        datetime.strptime(mes, "%Y-%m")             # solo para validar el formato
    except ValueError:
        return {"error": f"Mes inválido: {mes}. Usá el formato AAAA-MM."}

    return await db.resumen_gastos(user_id, mes)


async def consultar_inflacion(meses: int = 1) -> dict:
    """Devuelve la inflación mensual de Argentina (INDEC) de los últimos meses (ArgentinaDatos)."""

    # El modelo podría pedir 0 o 50 meses: se acota el valor al rango permitido.
    meses = max(1, min(int(meses), 12))

    # "async with" abre el cliente HTTP y lo cierra al terminar el bloque.
    async with httpx.AsyncClient(timeout=TIEMPO_ESPERA) as cliente_http:
        respuesta = await cliente_http.get("https://api.argentinadatos.com/v1/finanzas/indices/inflacion")
        respuesta.raise_for_status()                # convierte un código HTTP de error (4xx, 5xx) en excepción
        datos = respuesta.json()                    # lista de {"fecha": "AAAA-MM-DD", "valor": 2.7}

    # La lista viene ordenada de la más antigua a la más reciente:
    # [-meses:] toma las últimas.
    ultimos = datos[-meses:]

    # Inflación acumulada del período: los porcentajes no se suman, se componen.
    acumulado = 1.0
    for registro in ultimos:
        acumulado *= 1 + registro["valor"] / 100

    return {
        "fuente": "INDEC, vía ArgentinaDatos",
        "inflacion_mensual": [
            {"mes": registro["fecha"][:7], "variacion_pct": registro["valor"]}   # [:7] deja AAAA-MM
            for registro in ultimos
        ],
        "acumulada_pct": round((acumulado - 1) * 100, 1),
    }


async def obtener_cotizacion_dolar(tipo: str = "oficial") -> dict:
    """Devuelve la cotización del dólar en Argentina (DolarAPI)."""

    tipo = _normalizar(tipo)
    if tipo not in TIPOS_DOLAR:
        return {"error": f"Tipo de cotización desconocido: {tipo}", "tipos_disponibles": TIPOS_DOLAR}

    async with httpx.AsyncClient(timeout=TIEMPO_ESPERA) as cliente_http:
        respuesta = await cliente_http.get(f"https://dolarapi.com/v1/dolares/{tipo}")
        respuesta.raise_for_status()
        datos = respuesta.json()

    # Se devuelven solo los campos útiles para el modelo.
    return {
        "tipo": datos.get("nombre", tipo),
        "compra_ars": datos.get("compra"),
        "venta_ars": datos.get("venta"),
        "actualizado": datos.get("fechaActualizacion"),
    }


# ---------------------------------------------------------------------------
# 3. Declaraciones para el modelo
# ---------------------------------------------------------------------------

# Esto es lo único que Gemini conoce de cada función: su nombre, para qué sirve
# y qué parámetros recibe, descriptos con JSON Schema. El user_id NO se declara:
# lo agrega el bot, así el modelo nunca puede leer ni escribir gastos de otro usuario.
DECLARACIONES = [
    {
        "type": "function",
        "name": "registrar_gasto",
        "description": (
            "Guarda un gasto del usuario en su registro permanente. "
            "Usala cada vez que el usuario cuente que gastó, pagó o compró algo con un monto. "
            "Si en un mismo mensaje cuenta varios gastos, llamala una vez por cada gasto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "monto": {
                    "type": "number",
                    "description": "Monto en pesos argentinos, sin signos ni puntos de miles (por ejemplo, 8500).",
                },
                "categoria": {
                    "type": "string",
                    "enum": CATEGORIAS,     # el modelo solo puede elegir una de estas
                    "description": "Categoría del gasto. Si no encaja en ninguna, usá 'Otros'.",
                },
                "descripcion": {
                    "type": "string",
                    "description": "Detalle breve del gasto, en pocas palabras (por ejemplo, 'súper Carrefour', 'SUBE').",
                },
                "fecha": {
                    "type": "string",
                    "description": (
                        "Fecha del gasto en formato AAAA-MM-DD. Si el usuario dice 'ayer' o un día, "
                        "calculala a partir de la fecha de hoy. Si no menciona la fecha, no la envíes."
                    ),
                },
            },
            "required": ["monto", "categoria"],
        },
    },
    {
        "type": "function",
        "name": "consultar_gastos",
        "description": (
            "Devuelve los gastos registrados del usuario en un mes: total, total por categoría, "
            "cantidad y los últimos gastos. Usala cuando pregunte cuánto gastó, en qué se le va "
            "la plata o cuánto le queda del presupuesto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mes": {
                    "type": "string",
                    "description": "Mes en formato AAAA-MM. Si no lo especifica, no lo envíes (se usa el mes actual).",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "consultar_inflacion",
        "description": (
            "Obtiene la inflación mensual oficial de Argentina (INDEC) de los últimos meses "
            "y la acumulada del período. Usala cuando el usuario pregunte por la inflación "
            "o para comparar cuánto aumentaron sus gastos frente a los precios."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "meses": {
                    "type": "integer",
                    "description": "Cantidad de meses, de 1 a 12, contando desde el último publicado.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "obtener_cotizacion_dolar",
        "description": (
            "Obtiene la cotización actual del dólar estadounidense en pesos argentinos, "
            "con valores de compra y venta. Usala cuando el usuario pregunte cuánto está el dólar "
            "o cuente un gasto en dólares, para convertirlo a pesos antes de registrarlo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": TIPOS_DOLAR,
                    "description": (
                        "Tipo de cotización. Para suscripciones o compras pagadas con tarjeta "
                        "(Netflix, Spotify, compras online) usá 'tarjeta'; si no se especifica, 'oficial'."
                    ),
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# 4. Despachador
# ---------------------------------------------------------------------------

# Relaciona el nombre que usa el modelo con la función de Python que se ejecuta.
FUNCIONES = {
    "registrar_gasto": registrar_gasto,
    "consultar_gastos": consultar_gastos,
    "consultar_inflacion": consultar_inflacion,
    "obtener_cotizacion_dolar": obtener_cotizacion_dolar,
}

# Funciones que trabajan con los datos de un usuario: a estas el bot les agrega el user_id.
FUNCIONES_DEL_USUARIO = {"registrar_gasto", "consultar_gastos"}


async def ejecutar_funcion(nombre: str, argumentos: dict, user_id: int) -> dict:
    """Ejecuta la función pedida por el modelo y devuelve siempre un diccionario."""
    logger.info("El modelo pidió %s(%s)", nombre, argumentos)

    funcion = FUNCIONES.get(nombre)
    if funcion is None:                 # el modelo pidió una función que no existe
        return {"error": f"Función desconocida: {nombre}"}

    # Se copia el diccionario para no modificar el pedido original del modelo.
    argumentos = dict(argumentos or {})
    if nombre in FUNCIONES_DEL_USUARIO:
        # El user_id lo pone siempre el bot, nunca el modelo.
        argumentos["user_id"] = user_id

    try:
        # **argumentos desempaqueta el diccionario como parámetros con nombre:
        # {"meses": 3} se convierte en consultar_inflacion(meses=3).
        return await funcion(**argumentos)
    except Exception as error:
        # Cualquier falla (API caída, argumentos inválidos, tiempo agotado) se
        # informa al modelo como dato, para que pueda responder al usuario.
        logger.exception("Falló la herramienta %s", nombre)
        return {"error": f"No se pudo obtener el dato: {type(error).__name__}"}