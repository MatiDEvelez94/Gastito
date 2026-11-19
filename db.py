"""
Base de datos de Gastito (SQLite).

Tablas:
- mensajes:     historial de conversación (v3), que le da memoria al bot.
- gastos:       registro permanente de gastos de cada usuario (v4).
- analisis:     análisis mensuales con salida estructurada (v5).
- presupuestos: presupuesto mensual armado con el flujo guiado /presupuesto (v6).

Se utiliza aiosqlite, la versión asíncrona de sqlite3, para que las
operaciones sobre la base no bloqueen al bot mientras atiende a otros usuarios.
"""

import json                             # convierte listas a texto para guardarlas en SQLite

import aiosqlite                        # SQLite con soporte para async/await

# Nombre del archivo de la base de datos. Se crea en la carpeta desde la que
# se ejecuta el bot (la raíz del proyecto). Está excluido por .gitignore (*.db).
RUTA_DB = "gastito.db"

# Sentencias SQL que definen la estructura de la base.
# IF NOT EXISTS hace que puedan ejecutarse en cada arranque sin error: si la
# base ya existe de una versión anterior, solo se crean las tablas nuevas y
# los datos existentes quedan intactos.
ESQUEMA = """
CREATE TABLE IF NOT EXISTS mensajes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,                -- orden de llegada
    user_id INTEGER NOT NULL,                                 -- id de Telegram: separa a cada usuario
    rol     TEXT    NOT NULL CHECK (rol IN ('user', 'model')), -- quién escribió
    texto   TEXT    NOT NULL,                                 -- contenido del mensaje
    fecha   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP        -- fecha y hora de guardado
);
-- Índice que acelera la búsqueda de los mensajes de un usuario en orden.
CREATE INDEX IF NOT EXISTS idx_mensajes_usuario ON mensajes (user_id, id);

-- Gastos registrados (v4). Cada fila es un gasto de un usuario.
CREATE TABLE IF NOT EXISTS gastos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    monto       REAL    NOT NULL CHECK (monto > 0),           -- en pesos
    categoria   TEXT    NOT NULL,                             -- una de las 8 categorías
    descripcion TEXT    NOT NULL DEFAULT '',
    fecha       TEXT    NOT NULL,                             -- día del gasto, AAAA-MM-DD
    creado      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP    -- cuándo se registró
);
-- Índice que acelera la búsqueda de los gastos de un usuario por fecha.
CREATE INDEX IF NOT EXISTS idx_gastos_usuario_fecha ON gastos (user_id, fecha);

-- Análisis mensuales (v5). Cada columna corresponde a un campo del análisis estructurado.
CREATE TABLE IF NOT EXISTS analisis (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    mes                 TEXT    NOT NULL,                     -- AAAA-MM
    total_gastado       REAL    NOT NULL,
    estado              TEXT    NOT NULL,                     -- dentro del presupuesto, en riesgo, excedido o sin presupuesto
    categoria_principal TEXT    NOT NULL,
    porcentaje_principal INTEGER NOT NULL,                    -- de 0 a 100
    gastos_hormiga      TEXT    NOT NULL,                     -- lista guardada como texto JSON
    recomendaciones     TEXT    NOT NULL,                     -- lista guardada como texto JSON
    resumen             TEXT    NOT NULL,
    creado              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Presupuestos mensuales (v6). Un presupuesto por usuario y por mes:
-- UNIQUE hace que uno nuevo del mismo mes reemplace al anterior.
CREATE TABLE IF NOT EXISTS presupuestos (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mes     TEXT    NOT NULL,                                 -- AAAA-MM
    ingreso REAL    NOT NULL CHECK (ingreso > 0),             -- plata disponible en el mes
    ahorro  REAL    NOT NULL CHECK (ahorro >= 0),              -- meta de ahorro del mes
    creado  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, mes)
);
"""


async def inicializar() -> None:
    """Crea las tablas y los índices si todavía no existen."""
    # "async with" abre la conexión y garantiza que se cierre al terminar el
    # bloque, incluso si ocurre un error.
    async with aiosqlite.connect(RUTA_DB) as db:
        await db.executescript(ESQUEMA)  # executescript ejecuta varias sentencias SQL
        await db.commit()                # confirma los cambios en el archivo


# ---------------------------------------------------------------------------
# Historial de conversación (v3)
# ---------------------------------------------------------------------------

async def guardar_intercambio(user_id: int, pregunta: str, respuesta: str) -> None:
    """Guarda en una sola operación el mensaje del usuario y la respuesta del modelo."""
    async with aiosqlite.connect(RUTA_DB) as db:
        # executemany ejecuta la misma sentencia con cada tupla de la lista.
        # Los signos ? son parámetros: SQLite inserta los valores de forma
        # segura, lo que evita la inyección de SQL.
        await db.executemany(
            "INSERT INTO mensajes (user_id, rol, texto) VALUES (?, ?, ?)",
            [(user_id, "user", pregunta),      # primera fila: la pregunta
             (user_id, "model", respuesta)],   # segunda fila: la respuesta
        )
        await db.commit()


async def obtener_historial(user_id: int, limite: int) -> list[tuple[str, str]]:
    """Devuelve los últimos `limite` mensajes del usuario, del más antiguo al más reciente."""
    async with aiosqlite.connect(RUTA_DB) as db:
        # ORDER BY id DESC: del más reciente al más antiguo.
        # LIMIT ?: solo los últimos `limite` mensajes.
        cursor = await db.execute(
            "SELECT rol, texto FROM mensajes WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limite),
        )
        # fetchall devuelve una lista de tuplas (rol, texto).
        filas = await cursor.fetchall()

    # reversed los invierte para que el modelo lea la conversación en orden
    # cronológico, y list convierte el resultado en una lista.
    return list(reversed(filas))


async def borrar_historial(user_id: int) -> int:
    """Borra la conversación de un usuario y devuelve cuántos mensajes eliminó.
    Los gastos, análisis y presupuestos NO se borran: son registros aparte."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute("DELETE FROM mensajes WHERE user_id = ?", (user_id,))
        await db.commit()
        # rowcount indica cuántas filas afectó la última sentencia.
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Gastos (v4)
# ---------------------------------------------------------------------------

async def guardar_gasto(user_id: int, monto: float, categoria: str, descripcion: str, fecha: str) -> int:
    """Guarda un gasto y devuelve su id."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute(
            "INSERT INTO gastos (user_id, monto, categoria, descripcion, fecha) VALUES (?, ?, ?, ?, ?)",
            (user_id, monto, categoria, descripcion, fecha),
        )
        await db.commit()
        # lastrowid es el id que SQLite le asignó a la fila recién insertada.
        return cursor.lastrowid


async def resumen_gastos(user_id: int, mes: str) -> dict:
    """Devuelve el total del mes (AAAA-MM), el total por categoría y los últimos gastos."""
    # LIKE '2026-11%' selecciona todas las fechas que empiezan con ese mes.
    patron = f"{mes}%"
    async with aiosqlite.connect(RUTA_DB) as db:
        # SUM suma los montos y COUNT cuenta las filas; fetchone trae una sola fila.
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(monto), 0) FROM gastos WHERE user_id = ? AND fecha LIKE ?",
            (user_id, patron),
        )
        cantidad, total = await cursor.fetchone()

        # GROUP BY agrupa las filas por categoría y suma cada grupo.
        cursor = await db.execute(
            "SELECT categoria, SUM(monto) AS subtotal FROM gastos "
            "WHERE user_id = ? AND fecha LIKE ? GROUP BY categoria ORDER BY subtotal DESC",
            (user_id, patron),
        )
        por_categoria = {categoria: subtotal for categoria, subtotal in await cursor.fetchall()}

        # Los 5 gastos más recientes del mes, para que el modelo pueda nombrarlos.
        cursor = await db.execute(
            "SELECT fecha, categoria, descripcion, monto FROM gastos "
            "WHERE user_id = ? AND fecha LIKE ? ORDER BY fecha DESC, id DESC LIMIT 5",
            (user_id, patron),
        )
        ultimos = [
            {"fecha": f, "categoria": c, "descripcion": d, "monto_ars": m}
            for f, c, d, m in await cursor.fetchall()
        ]

    return {"mes": mes, "cantidad": cantidad, "total_ars": total,
            "por_categoria_ars": por_categoria, "ultimos": ultimos}


async def gastos_del_mes(user_id: int, mes: str) -> list[dict]:
    """Devuelve todos los gastos de un mes, del más antiguo al más reciente (para /analizar)."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute(
            "SELECT fecha, categoria, descripcion, monto FROM gastos "
            "WHERE user_id = ? AND fecha LIKE ? ORDER BY fecha, id",
            (user_id, f"{mes}%"),
        )
        return [
            {"fecha": f, "categoria": c, "descripcion": d, "monto_ars": m}
            for f, c, d, m in await cursor.fetchall()
        ]


async def borrar_ultimo_gasto(user_id: int) -> dict | None:
    """Borra el último gasto registrado del usuario y lo devuelve (None si no había)."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute(
            "SELECT id, monto, categoria, descripcion FROM gastos WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        fila = await cursor.fetchone()
        if fila is None:
            return None
        await db.execute("DELETE FROM gastos WHERE id = ?", (fila[0],))
        await db.commit()
    return {"monto": fila[1], "categoria": fila[2], "descripcion": fila[3]}


async def borrar_gastos(user_id: int) -> int:
    """Borra todos los gastos de un usuario (se usa en las pruebas)."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute("DELETE FROM gastos WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Análisis mensual (v5)
# ---------------------------------------------------------------------------

async def guardar_analisis(user_id: int, mes: str, total: float, analisis) -> int:
    """Guarda un análisis mensual (un objeto AnalisisMes) y devuelve su id."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute(
            "INSERT INTO analisis (user_id, mes, total_gastado, estado, categoria_principal, "
            "porcentaje_principal, gastos_hormiga, recomendaciones, resumen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                mes,
                total,
                analisis.estado,
                analisis.categoria_principal,
                analisis.porcentaje_principal,
                # SQLite no tiene un tipo lista: se guarda como texto JSON.
                json.dumps(analisis.gastos_hormiga, ensure_ascii=False),
                json.dumps(analisis.recomendaciones, ensure_ascii=False),
                analisis.resumen,
            ),
        )
        await db.commit()
        return cursor.lastrowid


# ---------------------------------------------------------------------------
# Presupuesto mensual (v6, flujo guiado)
# ---------------------------------------------------------------------------

async def guardar_presupuesto(user_id: int, mes: str, ingreso: float, ahorro: float) -> None:
    """Guarda el presupuesto del mes. Si ya había uno para ese mes, lo reemplaza."""
    async with aiosqlite.connect(RUTA_DB) as db:
        # ON CONFLICT ... DO UPDATE: si ya existe la fila (mismo usuario y mes),
        # se actualizan sus valores en lugar de dar error.
        await db.execute(
            "INSERT INTO presupuestos (user_id, mes, ingreso, ahorro) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, mes) DO UPDATE SET ingreso = excluded.ingreso, "
            "ahorro = excluded.ahorro, creado = CURRENT_TIMESTAMP",
            (user_id, mes, ingreso, ahorro),
        )
        await db.commit()


async def obtener_presupuesto(user_id: int, mes: str) -> dict | None:
    """Devuelve el presupuesto del mes del usuario, o None si no armó uno."""
    async with aiosqlite.connect(RUTA_DB) as db:
        cursor = await db.execute(
            "SELECT ingreso, ahorro FROM presupuestos WHERE user_id = ? AND mes = ?",
            (user_id, mes),
        )
        fila = await cursor.fetchone()
    if fila is None:
        return None
    ingreso, ahorro = fila
    return {"ingreso_ars": ingreso, "meta_ahorro_ars": ahorro, "para_gastar_ars": ingreso - ahorro}