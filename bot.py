"""Telegram-бот кофейни «Fade In».

- свободные вопросы -> LLM через OpenRouter (основная модель + запасные при ошибках, в т.ч. 429);
- кнопка «Меню» -> текст из menu.md без ИИ;
- бронь: заявку можно написать одним сообщением (модель возвращает JSON, код его проверяет)
  или пройти по шагам кнопкой «Забронировать столик»; запись в bookings.csv после подтверждения.
"""
import csv
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import booking_rules as rules

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в логи попадает токен бота из URL
log = logging.getLogger("fade-in")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))  # Астана: UTC+5

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Основная модель + 4 запасные на случай 429 и других ошибок.
# Бесплатные модели на OpenRouter периодически меняются: список можно переопределить
# в .env переменной MODELS (id через запятую).
DEFAULT_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
    "qwen/qwen3-235b-a22b:free",
]
MODELS = [m.strip() for m in os.getenv("MODELS", "").split(",") if m.strip()] or DEFAULT_MODELS

MENU_TEXT = (BASE_DIR / "menu.md").read_text(encoding="utf-8")
SYSTEM_PROMPT = (BASE_DIR / "system_prompt.md").read_text(encoding="utf-8").replace("{{MENU}}", MENU_TEXT)
EXTRACT_PROMPT = (BASE_DIR / "booking_prompt.md").read_text(encoding="utf-8")
BOOKINGS_FILE = BASE_DIR / "bookings.csv"
BOOKING_FIELDS = ["created_at", "user_id", "username", "date", "time", "guests", "name", "phone"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

HISTORY_LIMIT = 8  # сколько последних сообщений отправлять модели

MAIN_KB = ReplyKeyboardMarkup([["Меню", "Афиша выходного дня"], ["Забронировать столик"]], resize_keyboard=True)
CANCEL_KB = ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
CONFIRM_KB = ReplyKeyboardMarkup([["Подтверждаю", "Отмена"]], resize_keyboard=True)

LLM_FAIL_TEXT = "Плёнка порвалась, чиним! Попробуйте чуть позже."


# ---------- Меню и афиша (без ИИ) ----------

def format_menu() -> str:
    lines = []
    for raw in MENU_TEXT.splitlines():
        line = raw.rstrip()
        if line.startswith("### "):
            lines += ["", line[4:].strip() + ":"]
        elif line.startswith("## "):
            lines += ["", line[3:].strip().upper()]
        elif line.startswith("# "):
            lines.append(line[2:].strip())
        elif line.startswith("- "):
            lines.append("• " + line[2:])
        else:
            lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


WEEKEND_DAYS = {5: "Суббота", 6: "Воскресенье"}  # номера дней недели по datetime.weekday()


def weekend_program() -> dict[int, list[str]]:
    """Афиша выходного дня из раздела «Афиша выходного дня» файла menu.md: {5: [...], 6: [...]}."""
    program: dict[int, list[str]] = {}
    in_section, current = False, None
    for raw in MENU_TEXT.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_section = line[3:].strip().lower().startswith("афиша")
            current = None
        elif in_section and line.startswith("### "):
            title = line[4:].strip().lower()
            current = 5 if title.startswith("суббот") else 6 if title.startswith("воскрес") else None
            if current is not None:
                program[current] = []
        elif in_section and current is not None and line.startswith("- "):
            program[current].append(line[2:].strip())
    return program


PROGRAM = weekend_program()


def format_afisha() -> str:
    if not PROGRAM:
        return "Афишу ещё не расклеили. Загляните позже."
    parts = ["Афиша выходного дня. Приходите смотреть кино: у каждого фильма есть свой напиток."]
    for weekday, title in WEEKEND_DAYS.items():
        if PROGRAM.get(weekday):
            parts += ["", f"{title}:", *[f"• {item}" for item in PROGRAM[weekday]]]
    parts += ["", "Столик на сеанс — кнопка «Забронировать столик»."]
    return "\n".join(parts)


def afisha_hint(day) -> str:
    """Фраза про кино для сообщения о принятой брони: фильмы этого дня или приглашение на выходные."""
    items = PROGRAM.get(day.weekday())
    if items:
        shows = "; ".join(item.split(" (")[0] for item in items)
        return f" А в этот день у нас кино: {shows}. Приходите, места «ряд и место» ждут."
    return " А в выходные у нас кино: загляните в «Афишу выходного дня»."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "«Fade In»: кофе, кино и ни одного спойлера. Начинаем с первого глотка! "
        "В выходные у нас афиша: жмите «Афиша выходного дня» и выбирайте фильм.",
        reply_markup=MAIN_KB,
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_menu(), reply_markup=MAIN_KB)


async def show_afisha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_afisha(), reply_markup=MAIN_KB)


# ---------- LLM ----------

async def ask_llm(
    client: httpx.AsyncClient,
    messages: list[dict],
    temperature: float = 0.4,  # ниже температура — меньше опечаток и «творчества»
    max_tokens: int = 600,
) -> tuple[str | None, str | None]:
    """Перебирает модели по очереди. Любая ошибка (429, 5xx, таймаут, пустой ответ) -> следующая."""
    if not OPENROUTER_API_KEY:
        log.error("OPENROUTER_API_KEY не задан в .env")
        return None, None
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    for model in MODELS:
        try:
            resp = await client.post(
                OPENROUTER_URL,
                headers=headers,
                json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                timeout=45,
            )
        except httpx.HTTPError as exc:
            log.warning("%s: сетевая ошибка %s, пробую следующую", model, type(exc).__name__)
            continue
        if resp.status_code != 200:
            log.warning("%s: HTTP %s, пробую следующую", model, resp.status_code)
            continue
        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            log.warning("%s: неожиданный формат ответа, пробую следующую", model)
            continue
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
        if text:
            return text, model
        log.warning("%s: пустой ответ, пробую следующую", model)
    return None, None


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    history: list[dict] = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": update.message.text})
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[-HISTORY_LIMIT:]]
    answer, model = await ask_llm(context.application.bot_data["http"], messages)
    if answer is None:
        history.pop()
        await update.message.reply_text(LLM_FAIL_TEXT, reply_markup=MAIN_KB)
        return
    log.info("ответ дала модель %s", model)
    history.append({"role": "assistant", "content": answer})
    del history[:-HISTORY_LIMIT]
    await update.message.reply_text(answer, reply_markup=MAIN_KB)


async def extract_booking(client: httpx.AsyncClient, text: str, now: datetime) -> dict | None:
    """Просит модель вернуть заявку в JSON. Одна повторная попытка, если JSON не разобрался."""
    system = EXTRACT_PROMPT.replace("{{TODAY}}", now.strftime("%Y-%m-%d")).replace("{{WEEKDAY}}", WEEKDAYS[now.weekday()])
    messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    for attempt in (1, 2):
        answer, model = await ask_llm(client, messages, temperature=0, max_tokens=800)
        data = rules.parse_json_object(answer)
        if data is not None:
            return data
        log.warning("заявка: JSON не разобран (попытка %s из 2)", attempt)
    return None


# ---------- Бронь ----------

DATE, TIME, GUESTS, NAME, PHONE, CONFIRM, REQUEST = range(7)
STATE_OF = {"date": DATE, "time": TIME, "guests": GUESTS, "name": NAME, "phone": PHONE}
QUESTIONS = {
    "date": "На какую дату? Пишите ДД.ММ (например, 25.12) или «сегодня»/«завтра».",
    "time": f"Во сколько? Принимаем с {rules.OPEN_TIME:%H:%M} до {rules.LAST_BOOKING_TIME:%H:%M}, например 19:30.",
    "guests": f"Сколько гостей? От {rules.MIN_GUESTS} до {rules.MAX_GUESTS}: массовку заранее посчитаем.",
    "name": "На чьё имя пишем в титрах?",
    "phone": "Телефон? Например, +7 900 123-45-67. Звонить будем только по делу, без спойлеров.",
}
YES_WORDS = {"да", "подтверждаю", "ок", "ага", "верно", "подтверждаем"}
NO_WORDS = {"нет", "отмена"}


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


async def advance(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = "") -> int:
    """Спрашивает первое недостающее поле. Если всё собрано — показывает итог и просит подтвердить."""
    b = context.user_data["booking"]
    for field in rules.FIELD_ORDER:
        if field not in b:
            await update.message.reply_text(prefix + QUESTIONS[field], reply_markup=CANCEL_KB)
            return STATE_OF[field]
    await update.message.reply_text(
        f"{prefix}Проверяем сценарий: {b['date']:%d.%m.%Y}, {b['time']:%H:%M}, гостей: {b['guests']}, "
        f"имя: {b['name']}, телефон: {b['phone']}. Подтверждаете?",
        reply_markup=CONFIRM_KB,
    )
    return CONFIRM


async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Забронировать столик» или /book: спрашиваем по шагам."""
    context.user_data["booking"] = {}
    return await advance(update, context, "Столик — отличный сюжет! ")


async def request_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Свободный текст про бронь/столик/заявку: разбираем его моделью в JSON."""
    context.user_data["booking"] = {}
    return await process_request(update, context)


async def got_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await process_request(update, context)


async def process_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    now = now_local()
    data = await extract_booking(context.application.bot_data["http"], update.message.text, now)
    if data is None:
        await update.message.reply_text(
            "Не смог разобрать заявку, режиссёр в смятении. Напишите её ещё раз одним сообщением: "
            "дата, время, число гостей, имя и телефон. Или нажмите «Отмена».",
            reply_markup=CANCEL_KB,
        )
        return REQUEST
    if data.get("intent") != "booking":
        # не заявка, а обычный вопрос — отвечаем как обычно
        context.user_data.pop("booking", None)
        await chat(update, context)
        return ConversationHandler.END
    booking, errors = rules.apply_extracted(data, now)
    context.user_data["booking"] = booking
    first_missing = next((f for f in rules.FIELD_ORDER if f not in booking), None)
    if first_missing and first_missing in errors:
        prefix = errors[first_missing] + " "
    elif booking:
        prefix = "Принял. "
    else:
        prefix = ""
    return await advance(update, context, prefix)


async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    b = context.user_data["booking"]
    now = now_local()
    try:
        b["date"] = rules.parse_date(update.message.text, now.date())
    except rules.BookingError as exc:
        await update.message.reply_text(str(exc))
        return DATE
    prefix = ""
    if "time" in b:  # время пришло раньше даты: перепроверяем с настоящей датой
        try:
            rules.parse_time(f"{b['time']:%H:%M}", b["date"], now)
        except rules.BookingError as exc:
            del b["time"]
            prefix = f"{exc} "
    return await advance(update, context, prefix)


async def got_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    b = context.user_data["booking"]
    try:
        b["time"] = rules.parse_time(update.message.text, b["date"], now_local())
    except rules.BookingError as exc:
        await update.message.reply_text(str(exc))
        return TIME
    return await advance(update, context)


async def got_guests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["booking"]["guests"] = rules.parse_guests(update.message.text)
    except rules.BookingError as exc:
        await update.message.reply_text(str(exc))
        return GUESTS
    return await advance(update, context)


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["booking"]["name"] = rules.parse_name(update.message.text)
    except rules.BookingError as exc:
        await update.message.reply_text(str(exc))
        return NAME
    return await advance(update, context)


async def got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["booking"]["phone"] = rules.normalize_phone(update.message.text)
    except rules.BookingError as exc:
        await update.message.reply_text(str(exc))
        return PHONE
    return await advance(update, context)


def save_booking(update: Update, b: dict) -> None:
    user = update.effective_user
    is_new = not BOOKINGS_FILE.exists()
    with BOOKINGS_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(BOOKING_FIELDS)
        writer.writerow([
            now_local().strftime("%Y-%m-%d %H:%M:%S"),
            user.id,
            user.username or "",
            b["date"].strftime("%d.%m.%Y"),
            b["time"].strftime("%H:%M"),
            b["guests"],
            b["name"],
            b["phone"],
        ])


async def got_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = re.sub(r"[^\w]", "", update.message.text.lower())
    if answer in NO_WORDS:
        return await cancel(update, context)
    if answer not in YES_WORDS:
        await update.message.reply_text("Подтверждаете или отменяем? Нажмите кнопку.", reply_markup=CONFIRM_KB)
        return CONFIRM
    b = context.user_data["booking"]
    if datetime.combine(b["date"], b["time"]) <= now_local():
        await update.message.reply_text("Пока вы думали, время ушло, плёнку не перемотать. Начнём заново.", reply_markup=MAIN_KB)
        context.user_data.pop("booking", None)
        return ConversationHandler.END
    save_booking(update, b)
    log.info("новая бронь: %s %s, гостей %s", b["date"], b["time"], b["guests"])
    await update.message.reply_text(
        f"Бронь принята: {b['date']:%d.%m.%Y} в {b['time']:%H:%M}, гостей: {b['guests']}, на имя {b['name']}. "
        f"Столик ждёт {rules.HOLD_MINUTES} минут после назначенного времени, потом уходит на следующий дубль."
        + afisha_hint(b["date"]),
        reply_markup=MAIN_KB,
    )
    context.user_data.pop("booking", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("booking", None)
    await update.message.reply_text("Ладно, отменили. Снято, все свободны!", reply_markup=MAIN_KB)
    return ConversationHandler.END


# ---------- Запуск ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Необработанная ошибка", exc_info=context.error)


async def post_init(app: Application) -> None:
    app.bot_data["http"] = httpx.AsyncClient()


async def post_shutdown(app: Application) -> None:
    await app.bot_data["http"].aclose()


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("В .env не задан TELEGRAM_BOT_TOKEN.")
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY не задан: меню и бронь по шагам работают, ИИ-ответы нет.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    text_in_dialog = filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^Отмена$")
    booking = ConversationHandler(
        entry_points=[
            CommandHandler("book", book_start),
            MessageHandler(filters.Regex(r"^Забронировать столик$"), book_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)(брон|столик|заявк)"), request_start),
        ],
        states={
            REQUEST: [MessageHandler(text_in_dialog, got_request)],
            DATE: [MessageHandler(text_in_dialog, got_date)],
            TIME: [MessageHandler(text_in_dialog, got_time)],
            GUESTS: [MessageHandler(text_in_dialog, got_guests)],
            NAME: [MessageHandler(text_in_dialog, got_name)],
            PHONE: [MessageHandler(text_in_dialog, got_phone)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_confirm)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(r"^Отмена$"), cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.Regex(r"^Меню$"), show_menu))
    app.add_handler(CommandHandler("afisha", show_afisha))
    app.add_handler(MessageHandler(filters.Regex(r"^Афиша выходного дня$"), show_afisha))
    app.add_handler(booking)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_error_handler(on_error)

    log.info("Бот «Fade In» запущен. Модели: %s", ", ".join(MODELS))
    app.run_polling()


if __name__ == "__main__":
    main()
