"""
Flujo guiado del presupuesto mensual (/presupuesto).

Un ConversationHandler conduce al usuario paso a paso: cuánta plata tiene
para el mes, cuánto quiere ahorrar y la confirmación. En cada paso el bot
sabe qué dato espera porque la conversación tiene un ESTADO.

La IA interviene en un solo punto: interpretar montos escritos en lenguaje
natural ("200 lucas", "doscientos mil", "un 10 %"). Los números simples se
entienden con código, y todas las validaciones las hace el código.
"""

import logging                          # registro de eventos en la terminal
import re                               # expresiones regulares, para reconocer números simples

from pydantic import BaseModel, Field, ValidationError
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,                # handler que administra conversaciones por estados
    MessageHandler,
    filters,
)

import db                               # para guardar el presupuesto confirmado
from herramientas import hoy            # fecha de hoy en Argentina

logger = logging.getLogger("gastito")

# Estados de la conversación. range(3) asigna los números 0, 1 y 2: el
# valor no importa, solo que cada estado sea distinto.
INGRESO, AHORRO, CONFIRMACION = range(3)

MONTO_MAXIMO = 100_000_000              # límite razonable para un presupuesto personal


def formatear_pesos(monto: float) -> str:
    """Formatea un monto como $8.500 (punto para los miles, sin decimales)."""
    return "$" + f"{monto:,.0f}".replace(",", ".")


# ---------------------------------------------------------------------------
# 1. Interpretación del monto (código primero, IA si hace falta)
# ---------------------------------------------------------------------------

class MontoInterpretado(BaseModel):
    """Resultado que debe devolver el modelo al interpretar un monto."""
    entendido: bool = Field(description="true si el texto indica un monto o un porcentaje concreto; false si no.")
    valor: float = Field(description="El número indicado: el monto en pesos, o el porcentaje si es_porcentaje es true. 0 si no se entendió.")
    es_porcentaje: bool = Field(description="true si el usuario expresó un porcentaje (por ejemplo, 'el 10 %').")


INSTRUCCIONES_MONTO = """
Convertí el monto de dinero que menciona el usuario (en pesos argentinos) a un número.
Ejemplos: "200 lucas" = 200000; "doscientos mil" = 200000; "1,5 palos" = 1500000;
"nada" o "no quiero ahorrar" = 0; "el 10 %" = valor 10 con es_porcentaje true.
Si el texto no indica un monto concreto, respondé entendido=false.
""".strip()


def leer_numero_simple(texto: str) -> float | None:
    """Reconoce sin IA los montos escritos solo con números: 200000, $200.000, 200 000."""
    limpio = texto.replace("$", "").replace(".", "").replace(" ", "").strip()
    # fullmatch exige que TODO el texto sean dígitos (opcionalmente con decimales con coma).
    if re.fullmatch(r"\d+(,\d+)?", limpio):
        return float(limpio.replace(",", "."))
    return None


async def interpretar_monto(cliente, modelo: str, texto: str) -> tuple[float, bool] | None:
    """Devuelve (valor, es_porcentaje), o None si no se entendió."""
    numero = leer_numero_simple(texto)
    if numero is not None:
        return numero, False            # número simple: no hace falta consultar a la IA

    interaccion = await cliente.aio.interactions.create(
        model=modelo,
        system_instruction=INSTRUCCIONES_MONTO,
        input=texto,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": MontoInterpretado.model_json_schema(),
        },
        store=False,
    )
    resultado = MontoInterpretado.model_validate_json(interaccion.output_text)
    logger.info("Monto interpretado con IA: %r -> %s", texto, resultado.model_dump())
    if not resultado.entendido:
        return None
    return resultado.valor, resultado.es_porcentaje


async def pedir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[float, bool] | None:
    """Interpreta el mensaje como monto. Si falla, avisa al usuario y devuelve None."""
    await update.message.chat.send_action(ChatAction.TYPING)
    # El cliente de Gemini y el modelo se guardaron en bot_data al iniciar el bot.
    cliente = context.bot_data["cliente_gemini"]
    modelo = context.bot_data["modelo"]
    try:
        resultado = await interpretar_monto(cliente, modelo, update.message.text)
    except ValidationError:
        resultado = None                # respuesta del modelo con formato inválido
    except Exception:
        logger.exception("Error al interpretar el monto")
        await update.message.reply_text("No pude procesar el monto en este momento. Escribilo solo con números, por ejemplo 200000.")
        return None
    if resultado is None:
        await update.message.reply_text("No entendí el monto. Probá, por ejemplo, con \"200000\" o \"200 mil\".")
    return resultado


# ---------------------------------------------------------------------------
# 2. Pasos de la conversación
# ---------------------------------------------------------------------------

async def inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Paso 0 (/presupuesto): pide el ingreso del mes."""
    # context.user_data es un diccionario propio de cada usuario, que se
    # conserva entre mensajes. Ahí se van guardando los datos del presupuesto.
    context.user_data["presupuesto"] = {}
    mes = hoy().strftime("%Y-%m")
    anterior = await db.obtener_presupuesto(update.effective_user.id, mes)
    aviso = "\n(Ya tenés uno para este mes: el nuevo lo va a reemplazar.)" if anterior else ""
    await update.message.reply_text(
        f"¡Armemos tu presupuesto de {mes}! 💰{aviso}\n\n"
        "Paso 1 de 3: ¿cuánta plata tenés para este mes? Podés escribirlo como quieras: "
        "\"200000\", \"200 lucas\"...\n"
        "(Podés cancelar en cualquier momento con /cancelar)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return INGRESO                      # el próximo mensaje se procesa en el estado INGRESO


async def recibir_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Paso 1: valida el ingreso del mes."""
    resultado = await pedir_monto(update, context)
    if resultado is None:
        return INGRESO                  # dato inválido: se repite el mismo paso
    valor, es_porcentaje = resultado
    if es_porcentaje or not 0 < valor <= MONTO_MAXIMO:
        await update.message.reply_text("El ingreso tiene que ser un monto en pesos mayor que cero. ¿Cuánto tenés para el mes?")
        return INGRESO

    context.user_data["presupuesto"]["ingreso"] = valor
    await update.message.reply_text(
        f"Anotado: {formatear_pesos(valor)}.\n\n"
        "Paso 2 de 3: ¿cuánto querés ahorrar este mes? Puede ser un monto (\"30000\") "
        "o un porcentaje (\"el 15 %\"). Si no querés ahorrar, escribí \"nada\"."
    )
    return AHORRO


async def recibir_ahorro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Paso 2: valida la meta de ahorro y muestra el resumen para confirmar."""
    resultado = await pedir_monto(update, context)
    if resultado is None:
        return AHORRO
    valor, es_porcentaje = resultado
    presupuesto = context.user_data["presupuesto"]
    ingreso = presupuesto["ingreso"]

    if es_porcentaje:
        if not 0 <= valor < 100:
            await update.message.reply_text("El porcentaje tiene que estar entre 0 y 99. ¿Cuánto querés ahorrar?")
            return AHORRO
        valor = round(ingreso * valor / 100)    # el porcentaje se convierte a pesos

    # El código valida: el ahorro no puede ser negativo ni igualar o superar el ingreso.
    if not 0 <= valor < ingreso:
        await update.message.reply_text(
            f"El ahorro tiene que ser menor que tu ingreso ({formatear_pesos(ingreso)}). ¿Cuánto querés ahorrar?"
        )
        return AHORRO

    presupuesto["ahorro"] = valor
    teclado = ReplyKeyboardMarkup([["Confirmar", "Cancelar"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Paso 3 de 3: revisá tu presupuesto:\n\n"
        f"Ingreso del mes: {formatear_pesos(ingreso)}\n"
        f"Meta de ahorro: {formatear_pesos(valor)}\n"
        f"Para gastar: {formatear_pesos(ingreso - valor)}\n\n"
        "¿Confirmás?",
        reply_markup=teclado,
    )
    return CONFIRMACION


async def confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Paso 3: guarda el presupuesto si el usuario confirma."""
    if update.message.text not in ("Confirmar", "Cancelar"):
        await update.message.reply_text("Tocá \"Confirmar\" o \"Cancelar\" en el teclado, por favor.")
        return CONFIRMACION

    presupuesto = context.user_data.pop("presupuesto", {})   # pop lo quita de user_data
    if update.message.text == "Cancelar":
        await update.message.reply_text("Presupuesto cancelado.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END  # fin de la conversación

    mes = hoy().strftime("%Y-%m")
    await db.guardar_presupuesto(update.effective_user.id, mes, presupuesto["ingreso"], presupuesto["ahorro"])
    logger.info("Presupuesto guardado para %s (%s): %s", update.effective_user.id, mes, presupuesto)
    await update.message.reply_text(
        f"¡Listo! Tu presupuesto de {mes} quedó guardado. 🎉\n"
        f"Tenés {formatear_pesos(presupuesto['ingreso'] - presupuesto['ahorro'])} para gastar. "
        "Preguntame \"¿cuánto me queda?\" cuando quieras, o usá /analizar para ver cómo venís.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancelar: interrumpe el presupuesto en cualquier paso."""
    context.user_data.pop("presupuesto", None)
    await update.message.reply_text(
        "Presupuesto cancelado. ¿En qué más te ayudo?", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# 3. Armado del ConversationHandler
# ---------------------------------------------------------------------------

def crear_manejador() -> ConversationHandler:
    """Arma el ConversationHandler que se registra en bot.py."""
    texto = filters.TEXT & ~filters.COMMAND     # mensajes de texto que no son comandos
    return ConversationHandler(
        # Cómo empieza la conversación.
        entry_points=[CommandHandler("presupuesto", inicio)],
        # Qué handler atiende cada estado.
        states={
            INGRESO: [MessageHandler(texto, recibir_ingreso)],
            AHORRO: [MessageHandler(texto, recibir_ahorro)],
            CONFIRMACION: [MessageHandler(texto, confirmar)],
        },
        # Handlers que funcionan en cualquier estado.
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )