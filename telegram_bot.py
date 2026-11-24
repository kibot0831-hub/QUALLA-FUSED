"""Telegram: mensajes HTML, fotos y comandos (long polling), con reintentos y manejo de 429."""
import asyncio
import logging

import aiohttp

log = logging.getLogger("telegram")

MAX_LEN = 4000


def _split(text):
    if len(text) <= MAX_LEN:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


class Telegram:
    def __init__(self, token, chat_id, thread_id=""):
        self.token = token
        self.chat_id = str(chat_id)
        self.thread_id = thread_id
        self.enabled = bool(token and chat_id)
        self.session = None

    def _url(self, method):
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def start(self):
        if self.enabled:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        else:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID sin configurar: los mensajes solo se verán en los logs.")

    async def close(self):
        if self.session:
            await self.session.close()

    # ───────────── envío ─────────────
    async def send(self, text):
        """True si se entregó (o si Telegram está desactivado y solo se registra en logs)."""
        if not self.enabled:
            log.info("[Telegram desactivado]\n%s", text)
            return True
        ok = True
        for chunk in _split(text):
            ok = (await self._post(chunk)) and ok
            await asyncio.sleep(0.35)
        return ok

    async def _post(self, text):
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if self.thread_id:
            try:
                payload["message_thread_id"] = int(self.thread_id)
            except ValueError:
                pass
        for attempt in range(4):
            try:
                async with self.session.post(self._url("sendMessage"), json=payload) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200 and data.get("ok"):
                        return True
                    if r.status == 429:
                        wait = int(data.get("parameters", {}).get("retry_after", 5))
                        log.warning("Telegram 429, esperando %ss", wait)
                        await asyncio.sleep(wait + 1)
                        continue
                    if r.status == 400 and "parse_mode" in payload:
                        log.warning("Telegram rechazó el HTML (%s); reenvío en texto plano", data.get("description"))
                        payload.pop("parse_mode", None)
                        continue
                    log.error("Telegram error %s: %s", r.status, data)
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Telegram red: %s", e)
                await asyncio.sleep(2 * (attempt + 1))
        return False

    async def send_photo(self, png, caption=""):
        if not self.enabled:
            log.info("[Telegram desactivado] gráfico de %d bytes", len(png))
            return True
        for attempt in range(3):
            form = aiohttp.FormData()  # se consume al enviar: se recrea en cada intento
            form.add_field("chat_id", self.chat_id)
            if self.thread_id:
                form.add_field("message_thread_id", str(self.thread_id))
            if caption:
                form.add_field("caption", caption[:1000])
                form.add_field("parse_mode", "HTML")
            form.add_field("photo", png, filename="chart.png", content_type="image/png")
            try:
                async with self.session.post(self._url("sendPhoto"), data=form) as r:
                    data = await r.json(content_type=None)
                    if r.status == 200 and data.get("ok"):
                        return True
                    if r.status == 429:
                        await asyncio.sleep(int(data.get("parameters", {}).get("retry_after", 5)) + 1)
                        continue
                    log.error("Telegram sendPhoto error %s: %s", r.status, data)
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Telegram foto red: %s", e)
                await asyncio.sleep(2 * (attempt + 1))
        return False

    # ───────────── comandos (long polling) ─────────────
    async def poll_commands(self, handler, stop):
        """Lee mensajes del chat configurado y ejecuta `handler(texto) -> respuesta|None`. Ignora otros chats."""
        if not self.enabled:
            return
        try:  # si hubiera un webhook activo, getUpdates fallaría con 409
            async with self.session.post(self._url("deleteWebhook"), json={"drop_pending_updates": False}) as r:
                await r.read()
        except Exception:
            pass
        offset = None
        while not stop.is_set():
            try:
                body = {"timeout": 25, "allowed_updates": ["message", "channel_post"]}
                if offset is not None:
                    body["offset"] = offset
                async with self.session.post(self._url("getUpdates"), json=body,
                                             timeout=aiohttp.ClientTimeout(total=45)) as r:
                    data = await r.json(content_type=None)
                if not data.get("ok"):
                    log.warning("getUpdates: %s", data.get("description"))
                    await asyncio.sleep(10)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg or str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue  # seguridad: solo el chat configurado
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/"):
                        try:
                            reply = await handler(text)
                        except Exception as e:
                            log.exception("Error en comando %s", text)
                            reply = f"⚠️ Error ejecutando el comando: {type(e).__name__}"
                        if reply:
                            await self.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Polling de comandos: %s", e)
                await asyncio.sleep(5)
