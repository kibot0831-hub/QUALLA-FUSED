# Qullamaggie Fused v2 — bot de señales (BingX → Telegram)

Port del Pine v6 **"Qullamaggie Fused v2 - EP Long + Parabolic Short"**.
Cada vez que cierra una vela (15m por defecto) escanea **todos los perpetuos USDT cripto de BingX**
y avisa por Telegram. **Solo manda señales: no opera y no necesita API keys de BingX.**

## Qué te llega a Telegram
| Mensaje | Cuándo |
|---|---|
| 🟢 **EP LONG** / 🔴 **PARABOLIC SHORT** | Entrada: niveles (entrada, stop, TP1 a +1.8R), tamaño sugerido, contexto (cambio, gap, RVOL, ADR, OR, VWAP, EMA21 / subida, días verdes, extensión EMA20d), gatillo y plan de gestión |
| 🎯 TP1 · 🛑 Stop · ⚪ BE · 🔄 Trail EMA · ⏳ Tiempo | Seguimiento de cada señal abierta, con el R realizado |
| 👀 **RADAR** | Monedas recién "armadas" (EP) o con contexto parabólico activo y que aún esperan gatillo, con el nivel exacto a romper |
| 📊 Resumen diario | A las 00:00 UTC: señales del día anterior y R total |

## Archivos (todos en la raíz, sin carpetas)
`main.py` (bucle) · `strategy.py` (la estrategia) · `bingx.py` (datos públicos) · `telegram_bot.py` (mensajes)
· `state.py` (JSON persistente) · `config.py` (variables) · `test_bot.py` · `Dockerfile` · `railway.toml`
· `requirements.txt` · `.env.example` · `.gitignore` · `.dockerignore`

## Subir a GitHub
```bash
git init && git add . && git commit -m "Qullamaggie Fused v2 bot"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

## Railway
1. **New Project → Deploy from GitHub repo** → elige el repo. Railway detecta el `Dockerfile` (no usa mise ni `runtime.txt`).
2. **Variables**: añade `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`. El resto es opcional (ver `.env.example`; los valores por defecto son los inputs del Pine).
3. **Volume** (recomendado): servicio → *Settings → Volumes → Add* con mount path `/data`. Así las señales abiertas y la deduplicación sobreviven a los redeploys (el bot detecta el volumen solo).
4. No añadas *Custom Start Command* ni `Procfile`: el arranque es el `CMD` del Dockerfile.
5. En *Logs* verás una línea por ciclo: `Ciclo 12:15 UTC: 480/487 monedas · EP armados 3 · PS activos 0 · señales 1 …`

## Cómo se ha trasladado el Pine
- Solo velas **cerradas**. Contexto diario = valores `[1]` con días completos (cierre previo, ADR20, volumen medio 20d, EMA10/20, racha verde, mínimo de `PS_LOOKBACK` días).
- Día = **UTC** (`SESS_OFFSET_MIN=0`), Opening Range = primeros `OR_MINUTES`, RVOL a esta hora, ventana de entradas, máx. trades/día, stop estructural (día / OR / ATR×2) con filtro de ancho máximo en ADR, dimensionado por riesgo % con tope de nocional.
- El día actual se **reproduce vela a vela** en cada ciclo (EP armado, trades ya hechos, gatillos con cruce), igual que lo haría el Pine en el gráfico.
- Tamaño sugerido con `ACCOUNT_EQUITY` fijo (el Pine usa el equity del backtest, que va cambiando).
- Seguimiento de señales (convenciones prudentes): si una vela toca stop y TP1 a la vez, cuenta el stop; con gap se ejecuta en la apertura; tras TP1 el stop pasa a BE desde la vela siguiente; el trail diario se evalúa al abrir cada día UTC.
- Entrada = cierre de la vela de señal (precio de referencia a mercado). El Pine real entra en la apertura siguiente.

## Ajustes útiles
- `MIN_QUOTE_VOL_24H=0` analiza literalmente todas las monedas (más lento y más ruido en monedas muertas).
- Si el ciclo tarda más que el timeframe (aviso en logs): sube `API_RPS` (BingX limita las peticiones por IP; empieza con 8-10) o sube `MIN_QUOTE_VOL_24H`.
- `SETUPS=EP` o `SETUPS=PS` para activar solo un setup. `SEND_RADAR=false` si prefieres solo entradas.

## Tests (sin red)
```bash
pip install -r requirements.txt && python -m unittest -v test_bot
```
