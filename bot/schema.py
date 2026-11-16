"""Структура таблицы: названия листов и колонок. Должна совпадать с Google Таблицей."""

NUTRIENTS = ["kcal", "protein", "fat", "carbs", "sugar", "fiber",
             "na", "k", "ca", "mg", "fe", "zn", "p"]

NUTRIENT_NAMES = {
    "kcal": ("Калории", "ккал"), "protein": ("Белки", "г"), "fat": ("Жиры", "г"),
    "carbs": ("Углеводы", "г"), "sugar": ("Добавленный сахар", "г"), "fiber": ("Клетчатка", "г"),
    "na": ("Натрий", "мг"), "k": ("Калий", "мг"), "ca": ("Кальций", "мг"),
    "mg": ("Магний", "мг"), "fe": ("Железо", "мг"), "zn": ("Цинк", "мг"),
    "p": ("Фосфор", "мг"),
}

SHEET_ENTRIES = "Записи"
SHEET_DAYS = "Итоги"
SHEET_DICT = "Справочник"
SHEET_NORMS = "Нормы"
SHEET_SETTINGS = "Настройки"
SHEET_STATE = "Служебное"
SHEET_PURCHASES = "Покупки"
SHEET_INBOX = "Входящие"
SHEET_SUPPS = "Бады"

# Порядок колонок листа «Записи» (A..AG)
ENTRY_COLS = ["id", "day", "time", "kind", "text", "item", "place", "variant",
              "amount", "grams", *NUTRIENTS, "status", "question", "answers",
              "source", "msg_id", "question_msg_id", "file_id", "media_group", "options",
              "reply_msg_id"]

# Порядок колонок листа «Итоги» (A..S). C..P заполняются формулами.
DAY_COLS = ["day", "phase", *NUTRIENTS, "approx", "report_at", "last_reminder",
            "correction_at"]

# Порядок колонок листа «Справочник» (A..T)
DICT_COLS = ["place", "item", "variant", "portion", "grams", *NUTRIENTS,
             "source", "added"]

# Лист «Покупки» (A..E) — товары из чеков, подсказка для будущих записей
PURCHASE_COLS = ["date", "name", "brand", "amount", "grams"]

# Лист «Бады» (A..H): какие бады в какой приём и что из них идёт в нормы
SUPP_COLS = ["slot", "name", "count", "kcal", "fat", "mg", "zn", "note"]

STATE_KEYS = ["last_update_id", "current_day", "awaiting"]

# Статусы записей
NEW = "новая"            # ещё не разобрана ИИ
NEED_ASK = "уточнение"   # есть вопрос, ещё не отправлен
WAITING = "ждёт ответа"  # вопрос отправлен
OK = "ок"
APPROX = "примерно"      # уточнения не было, оценка
NOT_FOOD = "не еда"
ANSWER = "ответ"         # сообщение оказалось ответом на вопрос
RECEIPT = "чек"          # чек: покупки сохранены, в съеденное не идёт
CHANGED = "изменено"     # сообщение отредактировано, вместо строки — новая
OPEN = (NEW, NEED_ASK, WAITING)
FOOD_DONE = (OK, APPROX)

# Фазы дня
COLLECT = "сбор"
ASKING = "уточнение"
REPORTED = "отчёт"
CLOSED = "закрыт"


def col_letter(idx: int) -> str:
    """0 -> A, 26 -> AA"""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s
