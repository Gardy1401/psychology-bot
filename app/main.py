import asyncio
import html
import logging
import os
import re
import time
from typing import Dict, List

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

LOGGER = logging.getLogger("helpline_bot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


# ----------------- Константы и ресурсы -----------------

EMERGENCY_RESOURCES_HTML = (
    "<b>Если есть риск для жизни — немедленно звоните 112.</b>\n\n"
    "<b>Москва и МО — круглосуточно:</b>\n"
    "• Телефон неотложной психологической помощи: <b>051</b> (с городского) / <b>8&nbsp;(495)&nbsp;051</b> (с мобильного)\n"
    "• Экстренная медико-психологическая помощь (ПКБ №1 им. Алексеева): <b>+7&nbsp;(499)&nbsp;791-20-50</b>\n"
    "• Психологическая помощь МЧС: <b>+7&nbsp;(495)&nbsp;989-50-50</b>\n"
    "• Детский телефон доверия: <b>8-800-2000-122</b> или короткий <b>124</b> (с мобильного)\n"
    "• Универсальный номер экстренных служб РФ: <b>112</b>\n\n"
    "<i>Звонки анонимны там, где указано. Этот бот не заменяет профессиональную помощь.</i>"
)

# Ключевые слова высокого риска (самоповреждение/суицид и пр.)
HIGH_RISK_PATTERNS = [
    r"\bпокончу\b", r"\bсамоуби(й|ться|ваюсь)\b", r"\bне\s*хочу\s*жить\b",
    r"\bубить\s*себя\b", r"\bсвести\s*сч[её]ты\b", r"\bповешусь\b", r"\bрежу\s*себя\b",
    r"\bсуицид\b", r"\bсуицидальн\w+\b"
]
HIGH_RISK_RE = re.compile("|".join(HIGH_RISK_PATTERNS), re.IGNORECASE)

# Токсик-фильтр минимальный (спама/мат в проде расширяйте словарём)
TOXIC_PATTERNS = [r"\b(ненавижу всех|все уроды)\b"]
TOXIC_RE = re.compile("|".join(TOXIC_PATTERNS), re.IGNORECASE)


# ----------------- Клиент GigaChat -----------------

class GigaChatClient:
    AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    BASE_URL = "https://gigachat.devices.sberbank.ru/api"
    SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-Pro")
    TIMEOUT = aiohttp.ClientTimeout(total=30)

    def __init__(self):
        self._access_token = None
        self._expires_at = 0  # unix ms

        # Вариант 1: готовый базовый ключ (Base64(client_id:client_secret))
        self._auth_key = os.getenv("GIGACHAT_AUTH_KEY")

        # Вариант 2: client_id + client_secret -> соберём Basic сами
        cid = os.getenv("GIGACHAT_CLIENT_ID")
        csec = os.getenv("GIGACHAT_CLIENT_SECRET")
        if not self._auth_key and cid and csec:
            import base64
            self._auth_key = base64.b64encode(f"{cid}:{csec}".encode()).decode()

        if not self._auth_key:
            raise RuntimeError("GIGACHAT_AUTH_KEY или (GIGACHAT_CLIENT_ID + GIGACHAT_CLIENT_SECRET) не заданы")

    async def _ensure_token(self, session: aiohttp.ClientSession):
        now = int(time.time() * 1000)
        if self._access_token and now < self._expires_at - 15_000:
            return

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": os.popen("python - <<'PY'\nimport uuid; print(uuid.uuid4())\nPY").read().strip(),
            "Authorization": f"Basic {self._auth_key}",
        }
        data = {"scope": self.SCOPE}

        async with session.post(self.AUTH_URL, headers=headers, data=data) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GigaChat OAuth error {resp.status}: {text}")
            payload = await resp.json()
            self._access_token = payload["access_token"]
            self._expires_at = int(payload.get("expires_at", now + 25 * 60 * 1000))

    async def chat(self, messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
        async with aiohttp.ClientSession(timeout=self.TIMEOUT) as session:
            await self._ensure_token(session)
            url = f"{self.BASE_URL}/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            body = {
                "model": self.MODEL,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
            }
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"GigaChat chat error {resp.status}: {text}")
                data = await resp.json()
                return data["choices"][0]["message"]["content"]


# ----------------- Логика бота -----------------

SYSTEM_PROMPT = (
    "Ты — русскоязычный консультант по психологической поддержке. "
    "Действуй бережно и конкретно: уточняй, отражай чувства, предлагай шаги самопомощи, "
    "не давай диагнозов и юридических рекомендаций, не поощряй вред. "
    "Если заметен риск для жизни, мягко предложи обратиться за срочной помощью и набрать 112."
)

# Память чатов: chat_id -> список сообщений
DIALOGS: Dict[int, List[Dict[str, str]]] = {}
MAX_TURNS = 8

gigachat_client = None  # инициализируем в main()


def classify_message(text: str) -> str:
    if HIGH_RISK_RE.search(text or ""):
        return "high_risk"
    if TOXIC_RE.search(text or ""):
        return "toxic"
    return "ok"


def append_turn(chat_id: int, role: str, content: str):
    msgs = DIALOGS.setdefault(chat_id, [])
    msgs.append({"role": role, "content": content})
    # ограничение истории
    if len(msgs) > 2 * MAX_TURNS + 2:
        DIALOGS[chat_id] = msgs[-(2 * MAX_TURNS + 2):]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Привет. Я бот психологической поддержки. Пиши, что беспокоит. "
        "Команды: /help, /resources."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Как пользоваться:\n"
        "• Опиши ситуацию. Я отвечу поддержкой и идеями, как действовать.\n"
        "• Для срочной помощи см. /resources.\n"
        "• Конфиденциальность ограничена, не отправляй персональные данные."
    )


async def resources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(EMERGENCY_RESOURCES_HTML, disable_web_page_preview=True)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    chat_id = update.effective_chat.id

    # Мгновенная обработка высокого риска
    risk = classify_message(text)
    if risk == "high_risk":
        await update.message.reply_html(
            "Я вижу, что тема очень тяжёлая. Вот проверенные ресурсы помощи:\n\n" + EMERGENCY_RESOURCES_HTML,
            disable_web_page_preview=True,
        )
        # Дополнительно продолжим диалог мягким вопросом
        followup = "Если безопасно, напиши, где ты сейчас и кто рядом. Я постараюсь помочь."
        await update.message.reply_text(followup)
        return
    if risk == "toxic":
        await update.message.reply_text("Понимаю злость. Постараемся говорить уважительно, так будет продуктивнее.")

    # Диалог с GigaChat
    append_turn(chat_id, "system", SYSTEM_PROMPT) if not DIALOGS.get(chat_id) else None
    append_turn(chat_id, "user", text)

    msgs = DIALOGS[chat_id]
    try:
        reply = await gigachat_client.chat(msgs)
    except Exception as e:
        LOGGER.exception("GigaChat error: %s", e)
        reply = (
            "Не удалось обратиться к модели. Базовая поддержка:\n"
            "1) Замедлиться и подышать: 4 секунды вдох — 4 задержка — 6 выдох, 2–3 минуты.\n"
            "2) Выпиши, что именно тревожит, и что под твоим контролем.\n"
            "3) Обратись к близкому человеку. Если риск усиливается — набери 112 или 051.\n\n"
        )
    append_turn(chat_id, "assistant", reply)
    # Экранируем HTML для безопасности
    await update.message.reply_html(html.escape(reply))


def required_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return val


async def main():
    global gigachat_client
    telegram_token = required_env("TELEGRAM_TOKEN")
    gigachat_client = GigaChatClient()

    app = Application.builder().token(telegram_token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("resources", resources_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))

    await app.initialize()
    await app.start()
    LOGGER.info("Bot started")
    # выдерживаем вечный цикл до сигнала остановки
    await app.updater.start_polling()
    # graceful shutdown
    await app.updater.idle()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
