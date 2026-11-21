"""Проверки данных для брони столика. Без зависимостей, легко тестируется."""
import json
import re
from datetime import date, datetime, time, timedelta

OPEN_TIME = time(10, 0)
LAST_BOOKING_TIME = time(23, 0)  # закрываемся в 00:00, последняя бронь на 23:00
CLOSE_LABEL = "00:00"
HOLD_MINUTES = 15  # столько минут столик ждёт гостя после назначенного времени
MIN_GUESTS = 1
MAX_GUESTS = 8
MAX_DAYS_AHEAD = 60


class BookingError(ValueError):
    """Данные брони не прошли проверку. Текст ошибки можно показать гостю."""


def parse_date(text: str, today: date) -> date:
    t = text.strip().lower()
    if t == "сегодня":
        day = today
    elif t == "завтра":
        day = today + timedelta(days=1)
    elif t == "послезавтра":
        day = today + timedelta(days=2)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):  # формат ГГГГ-ММ-ДД из JSON
        try:
            day = date.fromisoformat(t)
        except ValueError:
            raise BookingError("Такой даты нет даже в сценарии. Попробуйте ещё раз.")
    else:
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}|\d{2}))?", t)
        if not m:
            raise BookingError("Дату пишите так: 25.12 или 25.12.2026. Можно «сегодня» или «завтра».")
        d, mo = int(m.group(1)), int(m.group(2))
        year = today.year
        if m.group(3):
            year = int(m.group(3))
            if year < 100:
                year += 2000
        try:
            day = date(year, mo, d)
        except ValueError:
            raise BookingError("Такой даты нет даже в сценарии. Попробуйте ещё раз.")
    if day < today:
        raise BookingError("Эта дата уже прошла, а назад в будущее мы пока не бронируем. Назовите другую.")
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise BookingError(f"Так далеко сценарий не расписан: бронь не дальше чем на {MAX_DAYS_AHEAD} дней вперёд.")
    return day


def parse_time(text: str, day: date, now: datetime) -> time:
    m = re.fullmatch(r"(\d{1,2})(?:[:.\-](\d{2}))?", text.strip())
    if not m:
        raise BookingError("Время пишите так: 19:30.")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise BookingError("Такого времени даже в кино не бывает. Пример: 19:30.")
    t = time(hour, minute)
    if t < OPEN_TIME or t > LAST_BOOKING_TIME:
        raise BookingError(
            f"Работаем с {OPEN_TIME:%H:%M} до {CLOSE_LABEL}, бронь принимаем на время "
            f"с {OPEN_TIME:%H:%M} до {LAST_BOOKING_TIME:%H:%M}: на титры не бронируем."
        )
    if day == now.date() and datetime.combine(day, t) <= now:
        raise BookingError("Это время уже прошло, плёнку не перемотать. Назовите позднее.")
    return t


def parse_guests(text: str) -> int:
    t = text.strip()
    if not t.isdigit():
        raise BookingError(f"Число гостей — цифрой, от {MIN_GUESTS} до {MAX_GUESTS}.")
    n = int(t)
    if not MIN_GUESTS <= n <= MAX_GUESTS:
        raise BookingError(
            f"Бронируем на {MIN_GUESTS}–{MAX_GUESTS} гостей: массовку такого размера лучше [уточнить] у администратора."
        )
    return n


def parse_name(text: str) -> str:
    t = " ".join(text.split())
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё '\-]{1,39}", t):
        raise BookingError("Имя — буквами, от 2 до 40 символов. В титрах цифры не принято.")
    return t


def normalize_phone(text: str) -> str:
    t = text.strip()
    if not re.fullmatch(r"\+?[\d\s\-()]{10,20}", t):
        raise BookingError("Телефон — 11 цифр, например +7 900 123-45-67.")
    digits = re.sub(r"\D", "", t)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    raise BookingError("В номере должно быть 11 цифр, например +7 900 123-45-67.")


# ---------- Разбор JSON-заявки от модели ----------

FIELD_ORDER = ["date", "time", "guests", "name", "phone"]


def parse_json_object(text: str | None) -> dict | None:
    """Достаёт JSON-объект из ответа модели. Возвращает None, если разобрать не удалось."""
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(t[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def apply_extracted(data: dict, now: datetime) -> tuple[dict, dict]:
    """Проверяет поля из JSON. Возвращает (корректные поля, {поле: текст ошибки}).

    Пустые и не прошедшие проверку поля в результат не попадают: их бот спросит у гостя.
    """
    booking: dict = {}
    errors: dict = {}

    def put(field: str, parser) -> None:
        value = data.get(field)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if value is None or (isinstance(value, str) and not value.strip()):
            return
        try:
            booking[field] = parser(str(value))
        except BookingError as exc:
            errors[field] = str(exc)

    put("date", lambda s: parse_date(s, now.date()))
    # без валидной даты время проверяем «на завтра», повторная проверка будет, когда дата появится
    day = booking.get("date", now.date() + timedelta(days=1))
    put("time", lambda s: parse_time(s, day, now))
    put("guests", parse_guests)
    put("name", parse_name)
    put("phone", normalize_phone)
    return booking, errors
