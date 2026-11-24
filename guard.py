"""Страж правил: помогает боту строго соблюдать system_prompt.md. Без зависимостей.

Алгоритм (его вызывает bot.py):
  1. pregate(): до модели. Просьба вне темы или попытка взломать промпт получает готовый отказ,
     модель не вызывается (правила 3 и 5).
  2. check(): после модели. Ответ проверяется кодом: цены и названия только из меню, время только
     из меню или из слов гостя, без эмодзи, списков, утечек промпта, обещаний «бронь принята»,
     не больше двух предложений (правила 1, 4, 5 и раздел «Формат ответа»).
  3. feedback(): если есть нарушения, модели возвращают её ответ и список нарушений. Так до двух раз.
  4. autofix(): если не помогло, чиним стиль кодом (эмодзи, списки, лишние предложения).
     Если осталось нарушение факта (цена, название, утечка), отправляется безопасный ответ SAFE_TEXT.
"""
import re
from dataclasses import dataclass
from itertools import combinations

MAX_SENTENCES = 2
MAX_CHARS = 400

SAFE_TEXT = "Тут я не хочу сочинять сценарий на ходу: лучше [уточнить] у администратора."
OFFTOPIC_TEXT = "Увы, у меня узкая роль: кофе, кино и брони. Давайте лучше про меню?"
INJECTION_TEXT = "Сценарий у меня один: кофе, кино и брони. Давайте лучше про меню или афишу?"

# «Названия в кавычках», которые бот может употреблять помимо названий из меню.
UI_QUOTES = {
    "забронировать столик", "подтверждаю", "отмена", "меню", "афиша выходного дня",
    "fade in", "ряд", "место", "ряд и место",
}


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    hard: bool  # True: неверный факт или утечка, одним форматированием не исправить


@dataclass(frozen=True)
class Facts:
    prices: frozenset
    names: frozenset
    times: frozenset


# ---------- Факты из menu.md ----------

def build_facts(menu_text: str) -> Facts:
    items: list[int] = []
    addons: list[int] = []
    in_addons = False
    for raw in menu_text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            in_addons = line[3:].strip().lower().startswith("добавк")
        elif line.startswith("- "):
            m = re.search(r"(\d+)\s*₽\s*$", line)
            if m:
                (addons if in_addons else items).append(int(m.group(1)))

    addon_sums = {0}
    for r in range(1, len(addons) + 1):
        addon_sums |= {sum(c) for c in combinations(addons, r)}
    singles = {p + a for p in items for a in addon_sums}
    prices = set(singles) | set(addons)
    prices |= {k * s for s in singles for k in range(2, 6)}        # несколько одинаковых позиций
    prices |= {a + b for a, b in combinations(sorted(singles), 2)}  # две разные позиции

    names = {n.casefold() for n in re.findall(r"«([^»]+)»", menu_text)} | UI_QUOTES
    times = set(re.findall(r"\b\d{1,2}:\d{2}\b", menu_text))
    return Facts(frozenset(prices), frozenset(names), frozenset(times))


# ---------- 1. До модели ----------

ON_TOPIC_RE = re.compile(
    r"(?i)\b(?:кофе|эспрессо|американо|капучино|латте|флэт|раф\b|какао|чай|лимонад|десерт|круассан|эклер|"
    r"чизкейк|попкорн|меню|афиш|фильм|кино|столик|брон|часы|добавк|молок)"
)
OFFTOPIC_RE = re.compile(
    r"(?i)биткоин|bitcoin|крипт|курс\s+(?:доллар|евро|валют|рубл|тенге)|погод|реферат|гороскоп|новост|"
    r"политик|президент|напиши\w*\s+(?:код|программ|скрипт|стих|сочинени)|\bpython\b|javascript|"
    r"домашн\w+\s+задани|реши\w*\s+(?:задач|уравнени)"
)
INJECTION_RE = re.compile(
    r"(?i)игнорир\w+\s+(?:(?:все|свои|предыдущ\w+|прошл\w+)\s+)*(?:инструкци|правил|промпт)|"
    r"(?:покажи|выведи|раскрой|повтори|напиши|пришли)\s+(?:(?:свой|твой|мне)\s+)*(?:system|систем\w+)\s*(?:prompt|промпт|инструкци)|"
    r"забудь\s+(?:(?:все|свои)\s+)*(?:инструкци|правил)|притворись"
)


def pregate(user_text: str) -> str | None:
    """Готовый отказ без вызова модели или None, если сообщение надо отдать модели."""
    if INJECTION_RE.search(user_text):
        return INJECTION_TEXT
    if OFFTOPIC_RE.search(user_text) and not ON_TOPIC_RE.search(user_text):
        return OFFTOPIC_TEXT
    return None


# ---------- 2. После модели ----------

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️‍]")
LIST_RE = re.compile(r"(?m)^\s*(?:[-*•–]\s+|\d+[.)]\s+|#{1,6}\s)")
MD_RE = re.compile(r"\*\*|__|`")
WANTS_LIST_RE = re.compile(r"(?i)меню|афиш|список|перечисл|прайс")
PRICE_RE = re.compile(r"(\d+)\s*(?:₽|руб)")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
QUOTE_RE = re.compile(r"«([^»]+)»")
LEAK_RE = re.compile(
    r"(?i)system\s*prompt|системн\w+\s+(?:промпт|инструкци)|мо[ийея]\s+инструкци|мо[ийея]\s+правил|"
    r"</?(?:ответ|размышление|примеры)>"
)
BOOKING_CLAIM_RE = re.compile(r"(?i)(?:бронь|столик|заявка)\s+(?:уже\s+)?(?:принят|оформлен|подтвержд|забронирован)\w*")


def count_sentences(text: str) -> int:
    return len([p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if re.search(r"\w", p)])


def check(answer: str, user_text: str, facts: Facts) -> list[Violation]:
    """Проверяет ответ гостю. Пустой список значит, что нарушений нет."""
    found: list[Violation] = []
    if not answer.strip():
        return [Violation("empty", "ответ пустой", True)]

    from_user_prices = {int(n) for n in PRICE_RE.findall(user_text)}
    from_user_times = set(TIME_RE.findall(user_text))

    for n in PRICE_RE.findall(answer):
        if int(n) not in facts.prices and int(n) not in from_user_prices:
            found.append(Violation("price", f"цены {n} ₽ нет в меню (цены только из меню, правила 1 и 4)", True))
            break
    for t in TIME_RE.findall(answer):
        if t not in facts.times and t not in from_user_times:
            found.append(Violation("time", f"времени {t} нет в меню (время только из меню, правило 1)", True))
            break
    for q in QUOTE_RE.findall(answer):
        if q.casefold() not in facts.names:
            found.append(Violation("name", f"названия «{q}» нет в меню (названия только из меню, правила 1 и 4)", True))
            break
    if LEAK_RE.search(answer):
        found.append(Violation("leak", "в ответе упомянуты инструкции или служебные теги (правило 5)", True))
    if BOOKING_CLAIM_RE.search(answer):
        found.append(Violation("booking", "нельзя говорить, что бронь принята: это сообщает только код", True))

    if EMOJI_RE.search(answer):
        found.append(Violation("emoji", "в ответе эмодзи, а нужно без эмодзи", False))
    if not WANTS_LIST_RE.search(user_text):
        if LIST_RE.search(answer) or MD_RE.search(answer):
            found.append(Violation("format", "в ответе список или разметка, а нужен обычный текст", False))
        if count_sentences(answer) > MAX_SENTENCES or len(answer) > MAX_CHARS:
            found.append(Violation("length", f"ответ длиннее {MAX_SENTENCES} предложений", False))
    return found


# ---------- 3. Повторная попытка ----------

def feedback(violations: list[Violation]) -> str:
    items = "; ".join(v.message for v in violations)
    return (
        f"Служебная проверка: ваш прошлый ответ нарушает правила: {items}. "
        "Перепишите ответ и исправьте нарушения. Не упоминайте эту проверку. "
        "Формат прежний: <размышление>…</размышление><ответ>…</ответ>."
    )


# ---------- 4. Исправление кодом ----------

def autofix(answer: str, user_text: str = "") -> str:
    """Чинит стиль: убирает эмодзи, разметку и маркеры списков, оставляет первые предложения.

    Если гость просил меню или афишу, список и длина не трогаются, убираются только эмодзи.
    """
    text = EMOJI_RE.sub("", answer)
    if WANTS_LIST_RE.search(user_text):
        return text.strip()
    text = MD_RE.sub("", text)
    text = re.sub(r"(?m)^\s*(?:[-*•–]\s+|\d+[.)]\s+|#{1,6}\s)", "", text)
    text = re.sub(r"\s+([.,!?…])", r"\1", " ".join(text.split()))  # «₽ .» после эмодзи -> «₽.»
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", text) if re.search(r"\w", p)]
    return " ".join(parts[:MAX_SENTENCES]).strip()
