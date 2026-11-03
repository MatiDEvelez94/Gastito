# Gastito 💸 · asistente de finanzas personales en Telegram

Proyecto final de Sistemas Inteligentes Aplicados (ITU UNCuyo).
**Versión actual: v1**: comandos básicos y eco, todavía sin IA.

## Estructura

```
gastito/
├── .venv/          entorno virtual (no se sube)
├── .env            secretos reales (no se sube)
├── .env.example    plantilla de secretos (sí se sube)
├── .gitignore      lo que Git ignora
├── requirements.txt
└── bot.py          el bot
```

## Cómo ponerlo en marcha (Windows)

### 1. Crear el bot en Telegram
1. En Telegram buscá **@BotFather** y mandale `/newbot`.
2. Nombre visible: `Gastito`.
3. Usuario (único, termina en `bot`): por ejemplo `GastitoFinanzasBot`.
4. Copiá el **token** que te da (`123456789:AAH...`). Es la contraseña del bot: no lo compartas.
5. Opcional: mandale `/setcommands`, elegí tu bot y pegá:
   ```
   start - Presentación de Gastito
   help - Lista de comandos
   categorias - Categorías de gasto
   ```

### 2. Preparar el proyecto (terminal de VS Code, en la carpeta `gastito`)
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
> Si PowerShell no deja activar el entorno: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` y repetí.

### 3. Cargar el token
Copiá `.env.example` como `.env` y reemplazá el valor:
```
TELEGRAM_TOKEN=123456789:AAH...tu_token
```

### 4. Ejecutar
```powershell
python bot.py
```
Tenés que ver `Gastito está en línea. Presioná Ctrl+C para detenerlo.`
Abrí el chat con tu bot y probá `/start`, `/help`, `/categorias` y cualquier texto.

## Si el token se publica por error
En @BotFather: `/revoke`, elegí el bot, y pegá el token nuevo en `.env`.
