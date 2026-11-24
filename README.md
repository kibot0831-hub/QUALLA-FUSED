# Qullamaggie Fused Bot — BingX → Telegram

Conversión a bot del script Pine **"Qullamaggie Fused v2 (EP Long + Parabolic Short)"**.
Escanea **todos los perpetuos USDT de BingX**, aplica la misma lógica vela a vela y envía **señales detalladas a Telegram**:
gráfico, entrada, stop, TP1, tamaño sugerido, contexto, niveles y avisos de TP1 / stop / BE / trail EMA / tiempo.

> ⚠️ El bot **solo envía señales**; no opera por ti y no necesita API keys de BingX (solo datos públicos).
> Información con fines educativos, no es asesoramiento financiero. Haz backtest y prueba en demo antes de arriesgar dinero.

## Archivos (todos en la raíz, sin carpetas)

| Archivo | Función |
|---|---|
| `main.py` | Bucle principal, monitor en vivo, comandos, dedupe |
| `strategy.py` | Réplica vela a vela de la estrategia Pine |
| `indicators.py` | EMA, ATR, SMA… equivalentes a Pine |
| `bingx.py` | Datos de BingX (ccxt) |
| `charts.py` | Gráfico PNG de cada entrada |
| `messages.py` | Formato de los mensajes |
| `telegram_bot.py` | Mensajes, fotos y comandos de Telegram |
| `config.py` | Parámetros por variables de entorno |
| `backtest.py` | Backtest con el mismo motor (se ejecuta en tu PC) |
| `requirements.txt`, `Dockerfile`, `railway.json` | Despliegue en Railway |
| `.env.example`, `.gitignore` | Plantilla de variables / ignorados |

## 1) Telegram

1. En Telegram habla con **@BotFather** → `/newbot` → copia el **token**.
2. Escribe cualquier mensaje a tu bot (o añádelo a tu grupo/canal como administrador).
3. Obtén tu **chat id**: abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca `"chat":{"id": ...}`
   (en grupos/canales es un número negativo, p. ej. `-100123...`).

## 2) GitHub

Crea un repositorio nuevo y sube **todos los archivos tal cual, en la raíz** (Add file → Upload files).
No subas ningún `.env` con tus claves.

## 3) Railway

1. [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** → elige el repositorio.
   Railway detecta el `Dockerfile` y `railway.json` (es un *worker*, no necesita puerto ni dominio).
2. Pestaña **Variables**: añade como mínimo `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`
   (el resto es opcional; ver `.env.example`).
3. **Settings → Region**: si en los logs ves errores 403/451 o "restricted location" al conectar con BingX,
   cambia la región del servicio (Europa o Singapur suelen funcionar).
4. Deploy. Debería llegarte **"🤖 Qullamaggie Fused Bot — iniciado"**.

*Opcional:* para conservar el registro de avisos entre reinicios, añade un **Volume** montado en `/data`
y la variable `STATE_PATH=/data/state.json`. Sin volumen funciona igual (tras un redeploy podrían repetirse
avisos de los últimos `MAX_EVENT_AGE_BARS` velas).

## Qué recibes

- **Entrada**: foto con velas, VWAP, EMA21, Opening Range, entrada, stop y TP1 + mensaje con operación, riesgo y tamaño,
  contexto (movimiento, RVOL, ADR, subida, verdes, extensión), niveles y enlaces a BingX/TradingView.
- **Seguimiento**: 🎯 TP1 · 🛑 Stop · ⚖️ BE · 🔔 Trail EMA · ⏱ Tiempo, con el resultado en R.
- **Alertas en vivo ⚡**: cada `LIVE_POLL_SEC` (20 s) se compara el precio en vivo con el stop y el TP1 de las posiciones
  abiertas, sin esperar al cierre de la vela. Se confirman después con la vela cerrada (sin duplicar el aviso).

## Comandos de Telegram

Solo responden en el chat configurado.

| Comando | Qué hace |
|---|---|
| `/status` | Estado, nº de candidatos, señales de hoy, posiciones abiertas |
| `/posiciones` | Posiciones virtuales abiertas (entrada, stop, TP1, estado) |
| `/watchlist` | Pares **armados** (EP) o con contexto Parabolic Short, con la distancia al gatillo |
| `/analiza BTC` | Diagnóstico de un par: qué condiciones cumple (✅/❌) y por qué no hay señal |
| `/config` | Parámetros activos |
| `/pausa` · `/reanudar` | Silenciar / reactivar las señales (el bot sigue analizando) |

## Backtest

Se ejecuta en tu ordenador con el **mismo motor** que el bot y las mismas variables de entorno:

```bash
pip install -r requirements.txt
python backtest.py --symbols BTC,ETH,SOL --days 14
python backtest.py --top 30 --days 30 --csv operaciones.csv
EP_MIN_RVOL=2 EP_TRIGGER=HH python backtest.py --top 30 --days 30     # probar parámetros
```

Muestra por setup: nº de operaciones, % de aciertos, % que llegan a TP1, R medio, R total, profit factor y drawdown máximo
(en múltiplos de R, descontando `--costs-pct`, 0.1 % por operación por defecto).
Si no hay operaciones, amplía los días/pares o relaja los umbrales.

## Cómo funciona

Cada cierre de vela del `TIMEFRAME` (por defecto 5m):

1. **Pre-filtro barato**: un único `fetch_tickers` + contexto diario en caché (descarga diaria una vez por día UTC) para
   quedarse solo con pares que *pueden* cumplir el contexto EP/PS (superconjunto conservador: nunca descarta un candidato válido).
2. **Replay exacto** de los candidatos (últimos `REPLAY_DAYS` días): Opening Range, VWAP por día, EMA21, ATR, RVOL a esa hora,
   `epArmed`, gatillos, stops estructurales, filtro ADR, `maxTradesDay`, parcial, BE, trail EMA diaria…
3. **Eventos nuevos → Telegram**, deduplicados. Si un envío falla, se reintenta en el siguiente ciclo.

El motor no guarda estado de la estrategia: se re-simula cada vez, así que se recupera solo tras un reinicio.

## Diferencias respecto a TradingView (conviene saberlas)

- **Entradas a mercado**: la señal sale al cierre de la vela confirmada; el precio mostrado es ese cierre (TradingView ejecuta en la apertura siguiente + slippage).
- Las alertas ⚡ usan el último precio del ticker (cada 20 s): pueden perderse una mecha entre dos consultas, que se detectaría al cierre de la vela. Coloca siempre el stop y el TP1 como órdenes en BingX al entrar.
- Si stop y objetivo caen en la misma vela se asume **stop primero** (conservador).
- Tamaño sugerido calculado con `ACCOUNT_EQUITY` fijo (en Pine usa el equity dinámico). Ajústalo al paso mínimo del contrato.
- **Seguimiento de posiciones virtuales** limitado a `REPLAY_DAYS` días.
- Los datos diarios asumen velas de 1d alineadas a **00:00 UTC** (como TradingView para cripto); el bot avisa en los logs si no lo parecen.
- Filtro extra opcional `MIN_QUOTE_VOLUME` (100 000 USDT/24h por defecto); ponlo a `0` para analizar absolutamente todo.
- Pares con menos de 35 días de histórico se omiten (el Pine daría `na`).

## Solución de problemas

- **No llega nada a Telegram**: revisa token/chat id y que hayas escrito antes al bot. En los logs aparece `Telegram error ...`.
- **Muy pocas señales**: es normal; EP exige ≥7 % + RVOL ≥1.5x y PS ≥50 % de subida. Usa `/watchlist` y `/analiza` para ver qué falta.
- **Probar rápido**: `SYMBOL_WHITELIST=BTC,ETH` y umbrales bajos (`EP_MIN_CHANGE=1`, `EP_MIN_ADR=1`, `EP_MIN_RVOL=0.5`).
- **Sin gráficos**: pon `SEND_CHARTS=false` si quieres reducir memoria/CPU.
