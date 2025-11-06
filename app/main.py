import html
import logging
import os
import re
import time
import uuid
from typing import Dict, List, Tuple

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

LOGGER = logging.getLogger("helpline_bot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# ----------------- Ресурсы и справка -----------------

EMERGENCY_RESOURCES_HTML = (
    "<b>Если есть риск для жизни — немедленно звоните 112.</b>\n\n"
    "<b>Москва и МО — круглосуточно:</b>\n"
    "• Телефон неотложной психологической помощи: <b>051</b> (с городского) / <b>8&nbsp;(495)&nbsp;051</b> (с мобильного)\n"
    "• Экстренная медико-психологическая помощь (ПКБ №1 им. Алексеева): <b>+7&nbsp;(499)&nbsp;791-20-50</b>\n"
    "• Психологическая помощь МЧС: <b>+7&nbsp;(495)&nbsp;989-50-50</b>\n"
    "• Детский телефон доверия: <b>8-800-2000-122</b> или короткий <b>124</b>\n"
    "• Универсальный номер экстренных служб РФ: <b>112</b>\n\n"
    "<i>Звонки анонимны там, где указано. Бот не заменяет профессиональную помощь.</i>"
)

SAFETY_FOOTER_HTML = (
    "\n\n<b>Если состояние ухудшается или есть риск — звоните 112 или 051, либо см. /resources.</b>"
)

# ----------------- Классификация риска -----------------

# «Немедленная опасность» — намерение/план, указание средств/места/времени
IMMINENT_PATTERNS = [
    r"\b(прямо\s*сейчас|сейчас|немедленно|сегодня)\b.*\b(прыгн[ул]|прыгну|спрыгну|сигану)\b",
    r"\b(иду|пойду|еду)\b.*\b(на\s*крыш[уеи]|к\W*мост[ууае])\b",
    r"\b(таблетк\w+|лекарств\w+|нож(ом)?|лезвие|петл(я|ю)|газ|верёвк\w+)\b.*\b(купил|наш[её]л|подготовил)\b",
    r"\bповешусь\b", r"\bперереж(у|ь)\s*вен(ы|ы)\b", r"\b(сделаю|совершу)\s*суицид\b",
]

# «Высокий риск» — выражение желания умереть/жить не хочет, без конкретного плана
HIGH_RISK_PATTERNS = [
    r"\bне\s*хоч[ую]\s*жить\b", r"\bхочу\s*умереть\b", r"\bустал\w*\s*жить\b",
    r"\b(покончу|покончить)\s*(с|со)\s*собой\b", r"\bсвести\s*сч[её]ты\s*с\s*жизнью\b",
    r"\bсуицид(?![а-я])\b", r"\bсуицидальн\w+\b", r"\bжизн[ьи]\s*нет\s*смысла\b",
]

# Самоповреждение без намерения умереть (NSSI)
NSSI_PATTERNS = [
    r"\b(режу|резал\w*)\s*себя\b", r"\bцарапаю\s*себя\b", r"\bжг(у|ал)\s*себя\b",
    r"\bбью\s*себя\b", r"\bсамоповрежден(и|ь)\w+\b",
]

# Третье лицо
THIRD_PERSON_PATTERNS = [
    r"\b(он|она|друг|подруга|сын|дочь|брат|сестра|муж|жена)\b.*\b(хочет|решил|собирается)\b.*\b(умереть|покончи(ть)|суицид)\b",
]

IMMINENT_RE = re.compile("|".join(IMMINENT_PATTERNS), re.IGNORECASE)
HIGH_RISK_RE = re.compile("|".join(HIGH_RISK_PATTERNS), re.IGNORECASE)
NSSI_RE = re.compile("|".join(NSSI_PATTERNS), re.IGNORECASE)
THIRD_RE = re.compile("|".join(THIRD_PERSON_PATTERNS), re.IGNORECASE)
MAX_TG_LEN = 4096
TOXIC_PATTERNS = [r"\b(ненавижу всех|все уроды)\b"]
TOXIC_RE = re.compile("|".join(TOXIC_PATTERNS), re.IGNORECASE)

def classify_message(text: str) -> Tuple[str, str]:
    """
    Возвращает (label, matched_phrase).
    label ∈ {imminent, high_risk, nssi, third_person, toxic, ok}
    """
    if IMMINENT_RE.search(text or ""):
        m = IMMINENT_RE.search(text or "")
        return "imminent", m.group(0)
    if HIGH_RISK_RE.search(text or ""):
        m = HIGH_RISK_RE.search(text or "")
        return "high_risk", m.group(0)
    if NSSI_RE.search(text or ""):
        m = NSSI_RE.search(text or "")
        return "nssi", m.group(0)
    if THIRD_RE.search(text or ""):
        m = THIRD_RE.search(text or "")
        return "third_person", m.group(0)
    if TOXIC_RE.search(text or ""):
        m = TOXIC_RE.search(text or "")
        return "toxic", m.group(0)
    return "ok", ""

# ----------------- Кризисные шаблоны (локальные, без LLM) -----------------

CRISIS_PRESETS = {
    "imminent": (
        "<b>Похоже, есть непосредственная опасность.</b>\n"
        "Если вы сейчас в небезопасном месте — <b>переместитесь в безопасное</b> и <b>наберите 112</b>.\n\n"
        "Можно сказать: «Мне нужна срочная психологическая помощь».\n\n"
        + EMERGENCY_RESOURCES_HTML
        + "\n\n<b>Могу спросить?</b> Вы один или рядом кто-то есть? Можете убрать потенциально опасные предметы и остаться на связи?"
    ),
    "high_risk": (
        "<b>Вижу, что очень тяжело.</b> Вам не нужно с этим оставаться одному. "
        "Сейчас ключевая цель — <b>снизить риск</b> и найти поддерживающий контакт.\n\n"
        + EMERGENCY_RESOURCES_HTML
        + "\n\nЕсли готовы, напишите, что сильнее всего давит и что помогало хотя бы немного раньше. Я отвечу поддержкой и простыми шагами."
    ),
    "nssi": (
        "<b>Похоже на самоповреждение без намерения умереть.</b> Это тоже риск. "
        "Попробуйте переключение на 20 минут: лед в ладонях, холодная вода, медленное дыхание 4-4-6, выйти на воздух при возможности.\n\n"
        + EMERGENCY_RESOURCES_HTML
        + "\n\nЕсли сможете, напишите, что запускает импульс. Разберём безопасные альтернативы."
    ),
    "third_person": (
        "<b>Если речь о другом человеке.</b> Проверьте, есть ли у него <b>непосредственная опасность</b>. "
        "Если да — вызывайте 112. Останьтесь с ним, уберите опасные предметы, говорите спокойно: "
        "«Я рядом. Давай позовём помощь».\n\n"
        + EMERGENCY_RESOURCES_HTML
        + "\n\nЕсли напишете возраст и где он сейчас, подберу более точные действия."
    ),
}

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

        self._auth_key = os.getenv("GIGACHAT_AUTH_KEY")
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
            "RqUID": str(uuid.uuid4()),
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

    async def send_html(update: Update, text_html: str):
        # режем по абзацам, чтобы не рвать теги
        parts = []
        current = []
        size = 0
        for para in text_html.split("\n\n"):
            add = (("\n\n" if current else "") + para)
            if size + len(add) > MAX_TG_LEN:
                parts.append("".join(current))
                current = [para]
                size = len(para)
            else:
                current.append(add if current else para)
                size += len(add)
        if current:
            parts.append("".join(current))

        for part in parts:
            try:
                await update.message.reply_html(part, disable_web_page_preview=True)
            except BadRequest as e:
                # если снова «Can't parse entities…», шлём как plain text
                try:
                    await update.message.reply_text(re.sub(r"<[^>]+>", "", part))
                except Exception:
                    LOGGER.exception("Failed to send fallback text for part")
                    raise

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

# ----------------- Диалог и правила -----------------

SYSTEM_PROMPT = (
    "Ты русскоязычный консультант по психологической поддержке. "
    "Цель: снизить стресс, повысить безопасность, предложить шаги самопомощи и направить к профессиональной помощи.\n"
    "Поведение:\n"
    "• Говори просто и бережно. Отражай чувства. Не ставь диагнозы. Не поощряй вред и употребление веществ.\n"
    "• Если слышны признаки риска — мягко рекомендуй немедленную помощь и номер 112. "
    "• Давай конкретные техники самопомощи: дыхание 4-4-6, заземление 5-4-3-2-1, план на ближайший час, связь с поддерживающим человеком.\n"
    "• В конце ответа добавляй краткий «футер безопасности» с номерами для Москвы и МО: 112, 051/8(495)051, ПКБ №1 +7(499)791-20-50, МЧС +7(495)989-50-50, Детский 8-800-2000-122.\n"
    "• Избегай запретов и морализаторства; фокус на поддержке и безопасности."
)

DIALOGS: Dict[int, List[Dict[str, str]]] = {}
MAX_TURNS = 8
gigachat_client = None
# --- рядом с DIALOGS ---
SAFE_CTX: Dict[int, str] = {}

def build_safe_summary(text: str) -> str:
    # Убираем конкретику методов и намерений
    redacted = IMMINENT_RE.sub("[описание непосредственной опасности удалено]", text)
    redacted = HIGH_RISK_RE.sub("[высокий риск]", redacted)
    redacted = NSSI_RE.sub("[самоповреждение]", redacted)
    # Короткое резюме для модели (без деталей и инструкций по методам)
    return (
        "Ранее выявлен высокий уровень дистресса/риск. "
        "Избегай обсуждения методов и деталей вреда. "
        "Фокус: поддержка, снижение риска, техники самопомощи, настаивание к обращению за помощью. "
        f"Сводка пользователя (очищено): {redacted[:500]}"
    )

def required_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return val

def append_turn(chat_id: int, role: str, content: str):
    msgs = DIALOGS.setdefault(chat_id, [])
    msgs.append({"role": role, "content": content})
    if len(msgs) > 2 * MAX_TURNS + 2:
        DIALOGS[chat_id] = msgs[-(2 * MAX_TURNS + 2):]

def render_with_footer(reply_text: str) -> str:
    # основной текст экранируем, футер — валидный HTML из whitelist Telegram
    safe = html.escape(reply_text)
    return safe + SAFETY_FOOTER_HTML

# ----------------- Хэндлеры -----------------

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

    label, matched = classify_message(text)
    if label in ("imminent", "high_risk", "nssi", "third_person"):
        SAFE_CTX[chat_id] = build_safe_summary(text)  # сохранить мост
        await update.message.reply_html(CRISIS_PRESETS[...], disable_web_page_preview=True)
        return


    if label == "toxic":
        await update.message.reply_text("Понимаю злость. Давайте говорить уважительно — так будет продуктивнее.")

    # Обычный диалог через GigaChat
# перед append_turn("user", text) и вызовом gigachat_client.chat(...)
    if not DIALOGS.get(chat_id):
        append_turn(chat_id, "system", SYSTEM_PROMPT)

    # если есть безопасный контекст — вшиваем отдельным системным сообщением один раз
    safe = SAFE_CTX.pop(chat_id, None)
    if safe:
        DIALOGS[chat_id].insert(0, {"role": "system", "content": safe})
    append_turn(chat_id, "user", text)
    try:
        reply = await gigachat_client.chat(DIALOGS[chat_id])
    except Exception as e:
        LOGGER.exception("GigaChat error: %s", e)
        reply = (
            "Не удалось обратиться к модели. Базовая поддержка:\n"
            "1) Дыхание 4-4-6 2–3 минуты.\n"
            "2) Запишите три конкретные задачи на ближайший час.\n"
            "3) Свяжитесь с близким. Если риск усиливается — 112 или 051.\n"
        )

    append_turn(chat_id, "assistant", reply)
    await update.message.reply_html(render_with_footer(reply), disable_web_page_preview=True)


    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        LOGGER.exception("Unhandled error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "Техническая ошибка при отправке сообщения. Попробуйте еще раз."
                )
            except Exception:
                pass
# ----------------- Запуск -----------------

def main():
    global gigachat_client
    telegram_token = required_env("TELEGRAM_TOKEN")
    gigachat_client = GigaChatClient()

    app = Application.builder().token(telegram_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("resources", resources_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    app.add_error_handler(on_error)
    app.run_polling()

if __name__ == "__main__":
    main()
