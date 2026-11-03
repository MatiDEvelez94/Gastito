"""Gastito v1 · asistente de finanzas personales en Telegram.

Versión 1 (Clase 1): esqueleto del bot, sin inteligencia artificial.
- /start      presentación
- /help       lista de comandos
- /categorias categorías de gasto que va a manejar Gastito
- eco         repite cualquier texto que no sea un comando
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("gastito")

# httpx registra cada petición a Telegram: lo bajamos a WARNING para no llenar la consola
logging.getLogger("httpx").setLevel(logging.WARNING)

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


# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Responde al comando /start
    nombre = update.effective_user.first_name
    logger.info("Nuevo /start de %s (id %s)", nombre, update.effective_user.id)
    await update.message.reply_text(
        f"¡Hola, {nombre}! Soy Gastito 💸, tu asistente de finanzas personales.\n"
        "Pronto vas a poder contarme tus gastos (\"gasté 8500 en el súper\") "
        "y yo los voy a ordenar por vos.\n\n"
        "Escribí /help para ver lo que sé hacer por ahora."
    )


# Comando /help
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Responde al comando /help
    await update.message.reply_text(
        "Estos son mis comandos:\n\n"
        "/start - Presentación de Gastito\n"
        "/help - Esta ayuda\n"
        "/categorias - Las categorías de gasto que manejo\n\n"
        "También podés escribirme cualquier mensaje y te lo repito."
    )


# Comando /categorias (propio del dominio)
async def categorias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Muestra las categorías en las que Gastito va a clasificar los gastos
    lista = "\n".join(CATEGORIAS)
    await update.message.reply_text(
        f"Estas son las categorías en las que voy a ordenar tus gastos:\n\n{lista}"
    )


# Responder un texto (función eco)
async def eco(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Repite cualquier mensaje de texto que no sea un comando
    texto = update.message.text
    logger.info("Mensaje recibido: %s", texto)
    await update.message.reply_text(f"Me dijiste: {texto}")


# Manejo de errores
async def manejo_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Registra cualquier error para que el bot no se detenga en silencio
    logger.error("Error al procesar una actualización", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Uy, algo salió mal 😅. Probá de nuevo en un rato."
        )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "Falta TELEGRAM_TOKEN. Copiá .env.example como .env y pegá tu token."
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", ayuda))
    app.add_handler(CommandHandler("categorias", categorias))
    # "es texto y no es un comando": así el eco nunca se queda con /start o /help
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, eco))
    app.add_error_handler(manejo_error)

    logger.info("Gastito está en línea. Presioná Ctrl+C para detenerlo.")

    # Polling: el bot le pregunta a Telegram, una y otra vez, si hay mensajes nuevos
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
