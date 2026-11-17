"""Gastito v4 · asistente de finanzas personales en Telegram.

Versión 4 (Clase 3): además de conversar y recordar, usa herramientas.
- /start      presentación
- /help       lista de comandos
- /categorias categorías de gasto
- /gastos     resumen de los gastos registrados en el mes
- /deshacer   borra el último gasto registrado
- /olvidar    borra la conversación (los gastos registrados se mantienen)
- texto libre lo responde Gemini, que puede registrar y consultar gastos,
              y consultar la inflación y el dólar
"""

import json                             # convierte los resultados de las funciones a texto JSON
import logging                          # registro de eventos en la terminal
import os                               # acceso a las variables de entorno

from dotenv import load_dotenv          # lee el archivo .env
from google import genai                # SDK oficial de Gemini
from telegram import Update             # representa cada novedad recibida de Telegram
from telegram.constants import ChatAction  # acciones de chat, como "escribiendo..."
from telegram.ext import (
    Application,                        # motor del bot: recibe y distribuye updates
    CommandHandler,                     # handler para comandos (/start, /help)
    ContextTypes,                       # tipo del parámetro context de los handlers
    MessageHandler,                     # handler para mensajes que no son comandos
    filters,                            # filtros para seleccionar qué mensajes atender
)

import db                               # módulo propio: acceso a la base SQLite
from herramientas import DECLARACIONES, ejecutar_funcion, hoy  # funciones propias del bot
from prompts import PROMPT_SISTEMA      # prompt de sistema definido en prompts.py

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# Carga en el entorno las variables del archivo .env.
load_dotenv()

# Lectura de las variables. os.getenv devuelve None si la variable no existe.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# El segundo argumento es el valor por defecto si GEMINI_MODEL no está en el .env.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Cantidad de mensajes anteriores que se envían al modelo en cada consulta
# (20 mensajes = 10 preguntas y sus respuestas).
MAX_MENSAJES_HISTORIAL = 20

# Cantidad máxima de vueltas del ciclo de function calling en un mismo mensaje.
# Evita un bucle infinito si el modelo pidiera funciones una y otra vez.
MAX_VUELTAS_HERRAMIENTAS = 5

# Longitud máxima de un mensaje de Telegram, en caracteres.
LIMITE_TELEGRAM = 4096

CATEGORIAS = [
    "🛒 Supermercado",
    "🍔 Comida y delivery",
    "🚌 Transporte",
    "🏠 Hogar y servicios",
    "🎓 Estudio",
    "🎉 Salidas y ocio",
    "💊 Salud",
    "📦 Otros",
]

# Formato de cada línea de registro: fecha y hora | nivel | origen | mensaje.
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx registra cada consulta HTTP; se limita a advertencias para no saturar la consola.
logging.getLogger("httpx").setLevel(logging.WARNING)
# Registrador propio del bot; sus mensajes aparecen con el nombre "gastito".
logger = logging.getLogger("gastito")

# ---------------------------------------------------------------------------
# Conexión con Gemini
# ---------------------------------------------------------------------------

# Se crea un único cliente para todo el programa.
cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)


def formatear_pesos(monto: float) -> str:
    """Formatea un monto como $8.500 (punto para los miles, sin decimales)."""
    return "$" + f"{monto:,.0f}".replace(",", ".")


def armar_entrada(historial: list[tuple[str, str]], mensaje_nuevo: str) -> list[dict]:
    """
    Convierte el historial guardado en la lista de pasos (steps) que espera
    la Interactions API y agrega al final el mensaje nuevo del usuario.
    """
    # Equivalencia entre el rol guardado en la base y el tipo de paso de la API.
    tipo_de_paso = {"user": "user_input", "model": "model_output"}

    # Un diccionario por cada fila del historial: tipo de paso y contenido.
    pasos = [
        {"type": tipo_de_paso[rol], "content": [{"type": "text", "text": texto}]}
        for rol, texto in historial
    ]

    # El último paso es siempre el mensaje que el usuario acaba de enviar.
    pasos.append({"type": "user_input", "content": [{"type": "text", "text": mensaje_nuevo}]})
    return pasos


async def consultar_gemini(historial: list[tuple[str, str]], mensaje: str, user_id: int) -> str:
    """
    Envía la conversación a Gemini y resuelve el ciclo de function calling:
    mientras el modelo pida funciones, se ejecutan y se le devuelven los
    resultados; cuando responde con texto, se devuelve ese texto.
    """
    # El modelo no sabe qué día es: se lo indicamos para que pueda registrar
    # gastos de "hoy" o "ayer" y consultar el mes correcto.
    instrucciones = f"{PROMPT_SISTEMA}\n\nHoy es {hoy().isoformat()} (AAAA-MM-DD)."

    # Lista de pasos que se envía al modelo. Empieza con el historial y el
    # mensaje nuevo, y crece dentro de este turno con los pedidos de funciones
    # y sus resultados. Esos pasos intermedios no se guardan en la base.
    entrada = armar_entrada(historial, mensaje)

    for vuelta in range(MAX_VUELTAS_HERRAMIENTAS):
        interaccion = await cliente_gemini.aio.interactions.create(
            model=GEMINI_MODEL,
            system_instruction=instrucciones,
            input=entrada,
            tools=DECLARACIONES,        # funciones que el modelo puede pedir
            store=False,
        )

        # Pasos de tipo function_call: el modelo pide ejecutar una función.
        # Puede pedir más de una en la misma respuesta (por ejemplo, dos gastos).
        llamadas = [paso for paso in (interaccion.steps or []) if paso.type == "function_call"]

        if not llamadas:
            # Sin pedidos de funciones: el modelo ya redactó la respuesta final.
            return interaccion.output_text or "No se me ocurrió nada para responder 🤔"

        # Se agregan a la entrada TODOS los pasos que devolvió el modelo, sin
        # modificarlos (incluidos los de razonamiento interno). La API los
        # necesita para relacionar cada resultado con su pedido.
        for paso in interaccion.steps:
            entrada.append(paso.model_dump())

        # Se ejecuta cada función pedida y se agrega su resultado como un paso
        # function_result. call_id debe coincidir con el id del pedido.
        for llamada in llamadas:
            resultado = await ejecutar_funcion(llamada.name, llamada.arguments, user_id)
            entrada.append({
                "type": "function_result",
                "call_id": llamada.id,
                "name": llamada.name,
                # ensure_ascii=False conserva tildes y ñ en lugar de códigos \u00f1
                "result": [{"type": "text", "text": json.dumps(resultado, ensure_ascii=False)}],
            })
        # Se repite el ciclo: el modelo recibe los resultados y decide si
        # necesita otra función o si ya puede responder.

    # Se agotaron las vueltas sin respuesta final.
    logger.warning("Se alcanzó el máximo de %d vueltas de herramientas", MAX_VUELTAS_HERRAMIENTAS)
    return "No pude completar la consulta. ¿Podés reformular la pregunta? 🙏"


def describir_error(error: Exception) -> str:
    """Traduce un error de la API a un mensaje comprensible para el usuario."""
    # Los errores HTTP de la API traen el código en el atributo status_code.
    codigo = getattr(error, "status_code", None)

    if codigo == 429:                    # 429: se superó la cuota de consultas
        return "Estoy recibiendo demasiadas consultas. Probá de nuevo en un minuto, por favor. ⏳"
    if codigo in (400, 401, 403):        # clave inválida, sin permiso o pedido mal armado
        return "Tengo un problema de configuración. Avisale a quien me administra. 🔧"
    # Cualquier otro caso: falla de red, error del servicio, modelo inexistente.
    return "Tuve un problema para pensar la respuesta. ¿Me lo preguntás de nuevo? 🙏"

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /start."""
    nombre = update.effective_user.first_name
    logger.info("Nuevo /start de %s (id %s)", nombre, update.effective_user.id)
    await update.message.reply_text(
        f"¡Hola, {nombre}! Soy Gastito 💸, tu asistente de finanzas personales.\n\n"
        "Contame tus gastos como me los contarías a un amigo, por ejemplo "
        "\"gasté 8500 en el súper\", y los voy registrando. Después te digo "
        "cuánto llevás gastado y te ayudo a armar tu presupuesto.\n\n"
        "Escribí /help para ver lo que sé hacer."
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /help."""
    await update.message.reply_text(
        "Estos son mis comandos:\n\n"
        "/start - Presentación de Gastito\n"
        "/help - Esta ayuda\n"
        "/categorias - Las categorías de gasto que manejo\n"
        "/gastos - Resumen de tus gastos del mes\n"
        "/deshacer - Borra el último gasto registrado\n"
        "/olvidar - Borra nuestra conversación (tus gastos quedan guardados)\n\n"
        "Y, sobre todo, podés contarme tus gastos o preguntarme cómo "
        "organizar tu plata. Me acuerdo de lo que hablamos, registro tus gastos "
        "y puedo consultar la inflación y la cotización del dólar.\n\n"
        "No me compartas datos sensibles como tarjetas, CBU o claves."
    )


async def categorias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra las categorías en las que Gastito clasifica los gastos."""
    lista = "\n".join(CATEGORIAS)
    await update.message.reply_text(
        f"Estas son las categorías en las que ordeno tus gastos:\n\n{lista}"
    )


async def gastos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /gastos: resumen del mes leído directo de la base, sin Gemini."""
    mes = hoy().strftime("%Y-%m")
    datos = await db.resumen_gastos(update.effective_user.id, mes)
    if datos["cantidad"] == 0:
        await update.message.reply_text("Todavía no registraste gastos este mes. Contame el primero 🙂")
        return

    lineas = [f"{categoria}: {formatear_pesos(subtotal)}"
              for categoria, subtotal in datos["por_categoria_ars"].items()]
    await update.message.reply_text(
        f"Tus gastos de {mes}:\n\n" + "\n".join(lineas) +
        f"\n\nTotal: {formatear_pesos(datos['total_ars'])} ({datos['cantidad']} gastos)"
    )


async def deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /deshacer: borra el último gasto registrado."""
    borrado = await db.borrar_ultimo_gasto(update.effective_user.id)
    if borrado is None:
        await update.message.reply_text("No tenés gastos registrados para borrar.")
        return
    await update.message.reply_text(
        f"Listo, borré {formatear_pesos(borrado['monto'])} en {borrado['categoria']}"
        + (f" ({borrado['descripcion']})." if borrado["descripcion"] else ".")
    )


async def olvidar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /olvidar: borra el historial del usuario (no los gastos)."""
    borrados = await db.borrar_historial(update.effective_user.id)
    logger.info("Usuario %s borró %d mensajes", update.effective_user.id, borrados)
    await update.message.reply_text(
        "Listo, empezamos de cero la charla. Tus gastos registrados siguen guardados (ver /gastos). 🙂"
    )


async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde con Gemini, teniendo en cuenta la conversación previa."""
    user_id = update.effective_user.id          # identifica de quién es la conversación
    texto = update.message.text
    logger.info("Mensaje de %s: %s", user_id, texto)

    # Muestra "escribiendo..." en el chat mientras se espera a Gemini.
    await update.message.chat.send_action(ChatAction.TYPING)

    # Paso 1: recuperar lo que ya se habló con este usuario.
    historial = await db.obtener_historial(user_id, MAX_MENSAJES_HISTORIAL)

    # Paso 2: consultar al modelo (con sus herramientas) con el historial más el mensaje nuevo.
    try:
        respuesta = await consultar_gemini(historial, texto, user_id)
    except Exception as error:
        logger.exception("Error al consultar a Gemini")          # traza completa, para el desarrollador
        await update.message.reply_text(describir_error(error))  # mensaje amable, para el usuario
        return

    # Paso 3: guardar la pregunta y la respuesta final (no los pasos intermedios).
    await db.guardar_intercambio(user_id, texto, respuesta)

    # Si la respuesta supera el límite de Telegram, se envía en partes.
    for inicio in range(0, len(respuesta), LIMITE_TELEGRAM):
        await update.message.reply_text(respuesta[inicio:inicio + LIMITE_TELEGRAM])


async def manejo_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra cualquier error no previsto para que el bot no se detenga en silencio."""
    logger.error("Error al procesar una actualización", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Uy, algo salió mal 😅. Probá de nuevo en un rato."
        )

# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------


async def al_iniciar(app: Application) -> None:
    """Se ejecuta una sola vez, antes de comenzar a recibir mensajes."""
    await db.inicializar()
    logger.info("Base de datos lista (%s).", db.RUTA_DB)


def main() -> None:
    # Lista con los nombres de las variables obligatorias que faltan en el .env.
    faltantes = [nombre for nombre, valor in [("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
                                              ("GEMINI_API_KEY", GEMINI_API_KEY)]
                 if not valor]
    if faltantes:
        raise SystemExit(f"Faltan variables en el .env: {', '.join(faltantes)}")

    # post_init registra una corrutina que se ejecuta al arrancar.
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(al_iniciar).build()

    # Registro de handlers. El orden importa: se usa el primero que coincide.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", ayuda))
    app.add_handler(CommandHandler("categorias", categorias))
    app.add_handler(CommandHandler("gastos", gastos))
    app.add_handler(CommandHandler("deshacer", deshacer))
    app.add_handler(CommandHandler("olvidar", olvidar))
    # Texto que no sea comando: lo responde Gemini.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
    app.add_error_handler(manejo_error)

    logger.info("Gastito v4 en línea con el modelo %s. Presioná Ctrl+C para detenerlo.", GEMINI_MODEL)
    # Polling: el bot consulta a Telegram, de forma continua, si hay mensajes nuevos.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()