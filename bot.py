"""Gastito v6 · asistente de finanzas personales en Telegram.

Versión 6 (Clase 4): salida estructurada y dos módulos avanzados.
- /start        presentación
- /help         lista de comandos
- /categorias   categorías de gasto
- /gastos       resumen de los gastos registrados en el mes
- /deshacer     borra el último gasto registrado
- /presupuesto  arma el presupuesto del mes paso a paso (flujo guiado)
- /cancelar     cancela el presupuesto en curso
- /analizar     análisis del mes con salida estructurada (v5)
- /olvidar      borra la conversación (los gastos registrados se mantienen)
- texto libre   lo responde Gemini, con herramientas
- fotos y voz   multimodalidad: tickets y notas de voz
"""

import base64                           # codifica fotos y audios como texto para enviarlos a Gemini
import json                             # convierte los resultados de las funciones a texto JSON
import logging                          # registro de eventos en la terminal
import os                               # acceso a las variables de entorno

from dotenv import load_dotenv          # lee el archivo .env
from google import genai                # SDK oficial de Gemini
from pydantic import ValidationError    # error que se produce si el JSON no cumple el esquema
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
import presupuesto                      # flujo guiado del presupuesto mensual (v6)
from analisis import analizar_mes       # análisis mensual con salida estructurada (v5)
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

# Duración máxima de un mensaje de voz, en segundos. Limita el tamaño del
# audio que se envía a Gemini (cada segundo de audio consume tokens).
DURACION_MAXIMA_VOZ = 60

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


def armar_entrada(historial: list[tuple[str, str]], contenido_nuevo: list[dict]) -> list[dict]:
    """
    Convierte el historial guardado en la lista de pasos (steps) que espera
    la Interactions API y agrega al final el contenido nuevo del usuario.
    contenido_nuevo es una lista de bloques: texto, imagen o audio.
    """
    # Equivalencia entre el rol guardado en la base y el tipo de paso de la API.
    tipo_de_paso = {"user": "user_input", "model": "model_output"}

    # Un diccionario por cada fila del historial. En el historial, el
    # contenido es siempre un único bloque de texto.
    pasos = [
        {"type": tipo_de_paso[rol], "content": [{"type": "text", "text": texto}]}
        for rol, texto in historial
    ]

    # El último paso es lo que el usuario acaba de enviar, tal como llegó:
    # ya es una lista de bloques, así que no se envuelve en otra lista.
    pasos.append({"type": "user_input", "content": contenido_nuevo})
    return pasos


async def consultar_gemini(
    historial: list[tuple[str, str]],
    contenido_nuevo: list[dict],          # lista de bloques (texto, imagen o audio)
    user_id: int,                         # para que las herramientas usen los datos de este usuario
    registro: list[str] | None = None,    # lista opcional donde se anotan las herramientas usadas
) -> str:
    """
    Envía la conversación a Gemini y resuelve el ciclo de function calling:
    mientras el modelo pida funciones, se ejecutan y se le devuelven los
    resultados; cuando responde con texto, se devuelve ese texto.
    Si se pasa una lista en `registro`, se agrega en ella el nombre de cada
    función que el modelo pidió.
    """
    # El modelo no sabe qué día es: se lo indicamos para que pueda registrar
    # gastos de "hoy" o "ayer" y consultar el mes correcto.
    instrucciones = f"{PROMPT_SISTEMA}\n\nHoy es {hoy().isoformat()} (AAAA-MM-DD)."

    # Lista de pasos que se envía al modelo. Crece dentro de este turno con
    # los pedidos de funciones y sus resultados (que no se guardan en la base).
    entrada = armar_entrada(historial, contenido_nuevo)

    for vuelta in range(MAX_VUELTAS_HERRAMIENTAS):
        interaccion = await cliente_gemini.aio.interactions.create(
            model=GEMINI_MODEL,
            system_instruction=instrucciones,
            input=entrada,
            tools=DECLARACIONES,        # funciones que el modelo puede pedir
            store=False,
        )

        # Pasos de tipo function_call: el modelo pide ejecutar una función.
        llamadas = [paso for paso in (interaccion.steps or []) if paso.type == "function_call"]

        if not llamadas:
            # Sin pedidos de funciones: el modelo ya redactó la respuesta final.
            return interaccion.output_text or "No se me ocurrió nada para responder 🤔"

        # Se agregan a la entrada TODOS los pasos que devolvió el modelo, sin
        # modificarlos. La API los necesita para relacionar cada resultado con su pedido.
        for paso in interaccion.steps:
            entrada.append(paso.model_dump())

        # Se ejecuta cada función pedida y se agrega su resultado como un paso
        # function_result. call_id debe coincidir con el id del pedido.
        for llamada in llamadas:
            if registro is not None:        # solo si se pasó una lista
                registro.append(llamada.name)
            resultado = await ejecutar_funcion(llamada.name, llamada.arguments, user_id)
            entrada.append({
                "type": "function_result",
                "call_id": llamada.id,
                "name": llamada.name,
                "result": [{"type": "text", "text": json.dumps(resultado, ensure_ascii=False)}],
            })

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
    if codigo == 503:                    # 503: los servidores de Gemini están saturados
        return "Gemini está con mucha demanda en este momento. Probá de nuevo en unos minutos. ⏳"
    # Cualquier otro caso: falla de red, error del servicio, modelo inexistente.
    return "Tuve un problema para pensar la respuesta. ¿Me lo preguntás de nuevo? 🙏"

# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /start."""
    nombre = update.effective_user.first_name
    logger.info("Nuevo /start de %s (id %s)", nombre, update.effective_user.id)
    await update.message.reply_text(
        f"¡Hola, {nombre}! Soy Gastito 💸, tu asistente de finanzas personales.\n\n"
        "Contame tus gastos como me los contarías a un amigo (\"gasté 8500 en el súper\"), "
        "mandame la foto del ticket o un audio, y los voy registrando. Armá tu presupuesto "
        "con /presupuesto y pedime un análisis del mes con /analizar.\n\n"
        "Escribí /help para ver todo lo que sé hacer."
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
        "/presupuesto - Armá tu presupuesto del mes paso a paso\n"
        "/cancelar - Cancela el presupuesto en curso\n"
        "/analizar - Análisis de cómo viene tu mes\n"
        "/olvidar - Borra nuestra conversación (tus gastos quedan guardados)\n\n"
        "También podés contarme tus gastos por texto, mandarme la foto de un ticket "
        "o un mensaje de voz. Puedo consultar la inflación y la cotización del dólar.\n\n"
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
    texto = (f"Tus gastos de {mes}:\n\n" + "\n".join(lineas) +
             f"\n\nTotal: {formatear_pesos(datos['total_ars'])} ({datos['cantidad']} gastos)")

    presupuesto_mes = await db.obtener_presupuesto(update.effective_user.id, mes)
    if presupuesto_mes:
        restante = presupuesto_mes["para_gastar_ars"] - datos["total_ars"]
        texto += f"\nTe quedan {formatear_pesos(restante)} de tu presupuesto."
    await update.message.reply_text(texto)


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
        "Listo, empezamos de cero la charla. Tus gastos y tu presupuesto siguen guardados. 🙂"
    )


async def analizar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /analizar: análisis del mes con salida estructurada, y lo guarda."""
    user_id = update.effective_user.id
    mes = hoy().strftime("%Y-%m")
    lista_gastos = await db.gastos_del_mes(user_id, mes)
    if not lista_gastos:
        await update.message.reply_text("Todavía no registraste gastos este mes, así que no hay nada para analizar 🙂")
        return
    presupuesto_mes = await db.obtener_presupuesto(user_id, mes)

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        analisis = await analizar_mes(cliente_gemini, GEMINI_MODEL, mes, lista_gastos, presupuesto_mes)
    except ValidationError:
        # El modelo respondió, pero el JSON no cumple el esquema.
        logger.exception("Análisis del mes con formato inválido")
        await update.message.reply_text("No pude armar el análisis. ¿Probás de nuevo? 🙏")
        return
    except Exception as error:
        logger.exception("Error al analizar el mes")
        await update.message.reply_text(describir_error(error))
        return

    total = sum(gasto["monto_ars"] for gasto in lista_gastos)
    numero = await db.guardar_analisis(user_id, mes, total, analisis)
    # Se muestra en la terminal el objeto completo que quedó guardado.
    logger.info("Análisis %d guardado en la base: %s", numero, analisis.model_dump_json())

    # El análisis es un objeto: sus campos se usan directamente, sin interpretar texto.
    emoji = {"dentro del presupuesto": "🟢", "en riesgo": "🟡",
             "excedido": "🔴", "sin presupuesto": "⚪"}[analisis.estado]
    hormiga = ", ".join(analisis.gastos_hormiga) if analisis.gastos_hormiga else "no detecté"
    recomendaciones = "\n".join(f"• {r}" for r in analisis.recomendaciones)
    texto = (
        f"Análisis de {mes} {emoji}\n\n"
        f"Estado: {analisis.estado}\n"
        f"Total gastado: {formatear_pesos(total)}\n"
        f"Donde más gastás: {analisis.categoria_principal} ({analisis.porcentaje_principal} %)\n"
        f"Gastos hormiga: {hormiga}\n\n"
        f"Recomendaciones:\n{recomendaciones}\n\n"
        f"{analisis.resumen}"
    )
    if analisis.estado == "sin presupuesto":
        texto += "\n\nArmá tu presupuesto con /presupuesto para saber si vas bien."
    await update.message.reply_text(texto)

# ---------------------------------------------------------------------------
# Mensajes: texto, fotos y voz
# ---------------------------------------------------------------------------


async def procesar_mensaje(update: Update, contenido: list[dict], texto_para_historial: str) -> None:
    """
    Pasos comunes a cualquier mensaje (texto, foto o voz): recuperar el
    historial, consultar al modelo, guardar el intercambio y responder.
    """
    user_id = update.effective_user.id          # identifica de quién es la conversación
    await update.message.chat.send_action(ChatAction.TYPING)

    # Paso 1: recuperar lo que ya se habló con este usuario.
    historial = await db.obtener_historial(user_id, MAX_MENSAJES_HISTORIAL)

    # Paso 2: consultar al modelo (con sus herramientas) con el historial más el contenido nuevo.
    try:
        respuesta = await consultar_gemini(historial, contenido, user_id)
    except Exception as error:
        logger.exception("Error al consultar a Gemini")          # traza completa, para el desarrollador
        await update.message.reply_text(describir_error(error))  # mensaje amable, para el usuario
        return                                                   # si falló, no se guarda nada

    # Paso 3: guardar el intercambio. En la base solo se guarda texto: para
    # fotos y audios se guarda una descripción breve, no el archivo.
    await db.guardar_intercambio(user_id, texto_para_historial, respuesta)

    # Paso 4: responder, en partes si supera el límite de Telegram.
    for inicio in range(0, len(respuesta), LIMITE_TELEGRAM):
        await update.message.reply_text(respuesta[inicio:inicio + LIMITE_TELEGRAM])


async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde un mensaje de texto."""
    texto = update.message.text
    logger.info("Mensaje de %s: %s", update.effective_user.id, texto)
    # El texto se convierte en una lista con un solo bloque. En el historial se guarda el mismo texto.
    await procesar_mensaje(update, [{"type": "text", "text": texto}], texto)


async def responder_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde una foto (por ejemplo, un ticket), con o sin texto (leyenda)."""
    # Telegram envía cada foto en varias resoluciones; la última es la más grande.
    foto = update.message.photo[-1]
    archivo = await foto.get_file()                    # datos para descargar la foto
    datos = await archivo.download_as_bytearray()      # la foto, como bytes

    # La leyenda es el texto que el usuario escribió junto con la foto (puede no haber).
    leyenda = update.message.caption or "Te mando esta foto. Si es un ticket, registrá el gasto."
    logger.info("Foto de %s (%d KB): %s", update.effective_user.id, len(datos) // 1024, leyenda)

    contenido = [
        {"type": "text", "text": leyenda},
        {
            "type": "image",
            # base64 representa bytes como texto, que es lo que admite el JSON de la API.
            # b64encode devuelve bytes; decode("ascii") los convierte en str.
            "data": base64.b64encode(datos).decode("ascii"),
            "mime_type": "image/jpeg",             # Telegram entrega las fotos en JPEG
        },
    ]
    await procesar_mensaje(update, contenido, f"[El usuario envió una foto] {leyenda}")


async def responder_voz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde un mensaje de voz."""
    voz = update.message.voice
    # Telegram informa la duración antes de descargar: se valida primero.
    if voz.duration > DURACION_MAXIMA_VOZ:
        await update.message.reply_text(
            f"Tu audio dura más de {DURACION_MAXIMA_VOZ} segundos. ¿Me mandás uno más corto? 🙏"
        )
        return

    archivo = await voz.get_file()
    datos = await archivo.download_as_bytearray()
    logger.info("Voz de %s (%d s)", update.effective_user.id, voz.duration)

    contenido = [
        # Instrucción en texto: indica al modelo qué hacer con el audio.
        {"type": "text", "text": "El usuario envió este mensaje de voz. Respondé a lo que dice."},
        {
            "type": "audio",
            "data": base64.b64encode(datos).decode("ascii"),
            "mime_type": "audio/ogg",              # Telegram entrega las notas de voz en OGG
        },
    ]
    await procesar_mensaje(update, contenido, "[El usuario envió un mensaje de voz]")


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

    # bot_data es un diccionario compartido por todos los handlers. Ahí se dejan
    # el cliente y el modelo para que presupuesto.py pueda usarlos sin importar bot.py.
    app.bot_data["cliente_gemini"] = cliente_gemini
    app.bot_data["modelo"] = GEMINI_MODEL

    # El flujo de presupuesto va PRIMERO: mientras está en curso, sus handlers
    # deben recibir los mensajes antes que el handler general de texto.
    app.add_handler(presupuesto.crear_manejador())

    # Comandos.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", ayuda))
    app.add_handler(CommandHandler("categorias", categorias))
    app.add_handler(CommandHandler("gastos", gastos))
    app.add_handler(CommandHandler("deshacer", deshacer))
    app.add_handler(CommandHandler("analizar", analizar))
    app.add_handler(CommandHandler("olvidar", olvidar))

    # Fotos y voz (multimodalidad), antes del handler de texto.
    app.add_handler(MessageHandler(filters.PHOTO, responder_foto))
    app.add_handler(MessageHandler(filters.VOICE, responder_voz))
    # Texto que no sea comando: lo responde Gemini.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
    app.add_error_handler(manejo_error)

    logger.info("Gastito v6 en línea con el modelo %s. Presioná Ctrl+C para detenerlo.", GEMINI_MODEL)
    # Polling: el bot consulta a Telegram, de forma continua, si hay mensajes nuevos.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


# Solo se arranca el bot si el archivo se ejecuta directamente (python bot.py),
# no si otro archivo lo importa.
if __name__ == "__main__":
    main()