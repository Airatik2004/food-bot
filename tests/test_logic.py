"""Офлайн-проверка логики: Telegram и Gemini заменены заглушками."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.logic import MSK, FoodBot, assign_day, hhmm  # noqa: E402
from bot.schema import NUTRIENTS  # noqa: E402
from bot.store import Store, day_formulas, to_cell  # noqa: E402
from bot.gemini import normalize_item, parse_json  # noqa: E402


def nutr(kcal):
    d = {k: 0 for k in NUTRIENTS}
    d.update(kcal=kcal, protein=kcal / 20, fat=kcal / 40, carbs=kcal / 10, na=kcal)
    return d


def item(name, kcal, complete=True, **kw):
    base = {"item": name, "place": kw.pop("place", "ВкусВилл"), "variant": "", "amount": "300 г",
            "grams": 300, "dict_id": None, "complete": complete,
            "question": None if complete else "Какой раф и объём?",
            "options": [] if complete else ["Классический 400 мл", "Солёная карамель 400 мл"],
            "source": "сайт", "nutrients": nutr(kcal)}
    base.update(kw)
    return normalize_item(base)


RECEIPT = [{"name": "Творог с малиной 5%", "brand": "Верейский", "amount": "140 г", "grams": 140},
           {"name": "Хлеб бородинский", "brand": "Коломенское", "amount": "300 г", "grams": 300}]


class FakeTG:
    def __init__(self):
        self.sent, self.mid = [], 1000

    def send(self, chat, text, reply_to=None, buttons=None):
        self.mid += 1
        self.sent.append({"id": self.mid, "text": text, "reply_to": reply_to, "buttons": buttons})
        return {"message_id": self.mid}

    def answer_callback(self, *a, **k):
        pass

    def edit_text(self, chat, mid, text):
        self.edits = getattr(self, "edits", []) + [(str(mid), text)]
        return True

    def download(self, fid):
        return b"data"


class FakeAI:
    def __init__(self):
        self.calls = 0
        self.last_history = ""
        self.last_purchases = ""

    def analyze(self, t, text, media, dct, open_q, history="", purchases=""):
        self.calls += 1
        self.last_history, self.last_purchases = history, purchases
        if text == "" and media:  # фото чека без подписи
            return {"transcript": "чек", "receipt": RECEIPT, "items": []}
        if "хлеб 4 куска" in text and media:
            return {"transcript": "чек", "receipt": RECEIPT,
                    "items": [item("Хлеб бородинский", 280, place="Коломенское", amount="4 куска")]}
        if "суп" in text:
            return {"transcript": text, "items": [item("Сырный суп", 300)]}
        if "раф" in text and "как вчера" in text:
            assert "Раф" in history, history
            return {"transcript": text, "items": [item("Раф", 380, place="Surf Coffee",
                                                       variant="солёная карамель", amount="400 мл")]}
        if "раф" in text:
            return {"transcript": text, "items": [item("Раф", 350, complete=False, place="Surf Coffee")]}
        if "сырники" in text:
            return {"transcript": text, "items": [item("Сырники", 450, place="дом")]}
        if "овсянка" in text:
            return {"transcript": text, "items": [item("Овсянка", 250, place="дом")]}
        if "ответ на сообщение бота" in text:
            assert "без тоста" in text and "Яичница с тостом" in text
            return {"transcript": text, "items": [item("Яичница (без тоста, с сыром)", 300, place="дом")]}
        if "че по белку подскажешь" in text:
            return {"transcript": text, "items": [], "request": {"mode": "eat", "wish": "добрать белок"}}
        if "Съел вариант из совета бота" in text:
            assert "Яичница" in text and "Омлет" not in text, text
            return {"transcript": text, "items": [item("Яичница с тостом", 250, place="дом")]}
        if "творог" in text:
            assert "Творог с малиной" in purchases
            return {"transcript": text, "items": [item("Творог с малиной 5%", 170, place="Верейский")]}
        if "плов" in text:
            return {"transcript": text, "items": [item("Плов", 800, complete=False,
                                                       place="Чайхона №1")]}
        if "гранатовый сок" in text:
            return {"transcript": text, "items": [item("Гранатовый сок", 150, place="", amount="250 мл")]}
        if "выпил сок 300 мл" in text:
            if "неполный" in text:
                return {"transcript": text, "items": [item("Сок", 140, complete=False, place="",
                                                           amount="300 мл")]}
            return {"transcript": text, "items": [item("Сок", 140, place="", amount="300 мл")]}
        if "пельмени как вчера" in text:
            ids = {l.split(" | ")[2]: int(l.split(" | ")[0]) for l in history.split("\n") if l}
            return {"transcript": text, "items": [item("Пельмени", 999, place="дом", amount="400 г",
                                                       grams=400, same_as_id=ids["Пельмени"])]}
        if text == "пельмени 400":
            return {"transcript": text, "items": [item("Пельмени", 800, place="дом", amount="400 г",
                                                       grams=400)]}
        if text == "boom":
            raise RuntimeError("Gemini не ответил: 503")
        if "вчера был он же" in text:
            ids = {l.split(" | ")[2]: int(l.split(" | ")[0]) for l in history.split("\n") if l}
            yid = ids["Гранатовый сок"]
            if "Сок" in ids and open_q:   # сегодняшний сок ждёт ответа → answer_to + уточнение вчерашнего
                return {"transcript": "бутылка Rich", "answer_to": int(open_q.split(" | ")[0]),
                        "items": [item("Гранатовый сок", 160, place="Rich", amount="250 мл", updates_id=yid)]}
            items = [item("Гранатовый сок", 160, place="Rich", amount="250 мл", updates_id=yid)]
            if "Сок" in ids:
                items.append(item("Гранатовый сок", 190, place="Rich", amount="300 мл",
                                  updates_id=ids["Сок"]))
            return {"transcript": "бутылка сока Rich", "items": items}
        if "вчера" in text:
            return {"transcript": text, "items": [item("Чипсы", 500, place="Lay's", yesterday=True)]}
        if "карамель" in text and open_q:
            first_id = int(open_q.split(" | ")[0])
            return {"transcript": text, "answer_to": first_id, "items": []}
        return {"transcript": text, "items": []}

    def suggest(self, mode="eat", **ctx):
        self.last_suggest = dict(ctx, mode=mode)
        return {"intro": "Добери белок", "outro": "Закроешь почти всё", "options": [
            {"title": "Омлет со шпинатом", "kind": "рецепт", "portion": "3 яйца", "time": "10 мин",
             "ingredients": ["яйца — 3 шт", "шпинат — 50 г"], "steps": ["взбей", "пожарь"],
             "kcal": 260, "protein": 20, "fat": 18, "carbs": 3, "why": "много белка"},
            {"title": "Яичница с тостом", "kind": "рецепт", "portion": "2 яйца + тост", "time": "7 мин",
             "ingredients": ["яйца — 2 шт", "хлеб — 30 г"], "steps": ["пожарь"],
             "kcal": 250, "protein": 14, "fat": 14, "carbs": 15, "why": "быстро"}]}

    def refine(self, text, it, hist, answer, media, dct, recent="", purchases=""):
        self.calls += 1
        return {"answer_text": answer,
                "item": item("Раф", 380, place="Surf Coffee", variant="солёная карамель",
                             amount="400 мл", grams=400)}


def msg(mid, dt, text, reply_to=None, photo=False, edited=False):
    m = {"message_id": mid, "date": int(dt.timestamp()), "chat": {"id": 42}}
    if photo:
        m["photo"] = [{"file_id": f"f{mid}"}]
        m["caption"] = text
    else:
        m["text"] = text
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"edited_message" if edited else "message": m}


def make():
    s = Store()
    s.norms = [{"key": "kcal", "value": 2945, "type": "цель", "unit": "ккал"},
               {"key": "protein", "value": 150, "type": "цель", "unit": "г"},
               {"key": "na", "value": 2000, "type": "не больше", "unit": "мг"},
               {"key": "fiber", "value": 30, "type": "не меньше", "unit": "г"}]
    clock = {"now": datetime(2026, 11, 8, 9, 0, tzinfo=MSK)}
    tg, ai = FakeTG(), FakeAI()
    bot = FoodBot(s, tg, ai, 42, now_fn=lambda: clock["now"])
    return s, tg, ai, bot, clock


def T(d, h, m=0):
    return datetime(2026, 11, d, h, m, tzinfo=MSK)


def run(bot, clock, when, *updates):
    clock["now"] = when
    for u in updates:
        bot.handle_update(u)
    bot.process_new()
    bot.tick()


def test_assign_day():
    ds = hhmm("05:00")
    assert assign_day(T(8, 9), ds) == "2026-11-08"
    assert assign_day(T(9, 0, 30), ds) == "2026-11-08"   # ночь → вчера
    assert assign_day(T(9, 4, 59), ds) == "2026-11-08"
    assert assign_day(T(9, 5, 0), ds) == "2026-11-09"
    assert hhmm(0.2083333) == hhmm("05:00")


def test_full_day():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 9, 1), msg(1, T(8, 9), "сырный суп из вкусвилла"))
    r = tg.sent[-1]
    assert r["text"].startswith("🍽") and "300 ккал" in r["text"] and r["reply_to"] == 1
    assert "📊 Сегодня:" in r["text"] and "Калории: 300 / 2945 ккал" in r["text"], r["text"]
    assert "Белки" in r["text"] and "Натрий" in r["text"] and "Клетчатка" in r["text"]

    run(bot, clock, T(8, 13, 1), msg(2, T(8, 13), "раф из серфа"))
    summary, q = tg.sent[-2], tg.sent[-1]
    assert "~350" in summary["text"] and "уточню ниже" in summary["text"]
    assert "≈ часть позиций примерные" in summary["text"]
    assert q["text"].startswith("❓") and q["buttons"][0][0][0] == "Классический 400 мл"
    raf = s.entry(2)

    # вечером бот ничего сам не пишет
    n = len(tg.sent)
    for h in (15, 21, 22, 23):
        run(bot, clock, T(8, h, 1))
    run(bot, clock, T(9, 1, 1))
    assert len(tg.sent) == n, "никаких вечерних сообщений"

    # ночной перекус → тот же день
    run(bot, clock, T(9, 0, 31), msg(3, T(9, 0, 30), "сырники"))
    assert s.entry(3)["day"] == "2026-11-08"
    assert "Калории: 1100 / 2945" in tg.sent[-1]["text"], tg.sent[-1]["text"]

    # 05:01 — итог за вчера, неотвеченный раф — «примерно»
    run(bot, clock, T(9, 5, 1))
    sm = tg.sent[-1]["text"]
    assert sm.startswith("🌅 Итог за вс, 8 ноября") and "Сырники" in sm and "≈" in sm, sm
    assert raf["status"] == "примерно" and s.day("2026-11-08")["phase"] == "закрыт"
    n = len(tg.sent)
    run(bot, clock, T(9, 9, 1))
    assert len(tg.sent) == n, "итог приходит один раз"

    # на вопрос можно ответить и после закрытия дня
    bot.handle_update({"callback_query": {
        "id": "c1", "data": f"o:{raf['id']}:1",
        "message": {"message_id": q["id"], "chat": {"id": 42}, "text": q["text"],
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "Классический 400 мл", "callback_data": f"o:{raf['id']}:0"}],
                        [{"text": "Солёная карамель 400 мл", "callback_data": f"o:{raf['id']}:1"}]]}}}})
    assert raf["status"] == "ок" and raf["kcal"] == 380
    assert "За вс, 8 ноября" in tg.sent[-1]["text"] and "Калории: 1130" in tg.sent[-1]["text"]


def test_history_and_answer_by_text():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 13, 1), msg(1, T(8, 13), "раф из серфа"))
    bot.handle_update(msg(2, T(8, 13, 5), "солёная карамель 400", reply_to=tg.sent[-1]["id"]))
    assert s.entry(1)["status"] == "ок"
    run(bot, clock, T(9, 9, 1), msg(3, T(9, 9), "раф как вчера"))
    assert "Surf Coffee" in ai.last_history
    assert s.entry(2)["status"] == "ок" and s.entry(2)["day"] == "2026-11-09"

    # ответ обычным сообщением (не реплаем)
    run(bot, clock, T(9, 14, 1), msg(4, T(9, 14), "плов из чайзоны"))
    run(bot, clock, T(9, 14, 3), msg(5, T(9, 14, 2), "солёная карамель 400"))
    assert s.entry(4)["status"] == "ответ" and s.entry(3)["status"] == "ок"


def test_receipt():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 15, 1), msg(1, T(9, 15), "", photo=True))
    assert len(s.purchases) == 2
    e = s.entry(1)
    assert e["status"] == "ждёт ответа" and e["source"] == "чек"
    q = tg.sent[-1]
    assert q["text"].startswith("🧾") and "Что из этого" in q["text"]
    assert q["buttons"][0][0][0] == "Ничего не ел"
    assert "Итого" not in "".join(x["text"] for x in tg.sent), "чек не идёт в съеденное"
    run(bot, clock, T(9, 15, 3), msg(2, T(9, 15, 2), "творог весь", reply_to=q["id"]))
    assert e["status"] == "чек"
    food = [x for x in s.entries if x["status"] == "ок"]
    assert len(food) == 1 and food[0]["item"].startswith("Творог")
    assert "Калории: 170 / 2945" in tg.sent[-1]["text"]

    # чек с подписью о съеденном
    run(bot, clock, T(9, 16, 1), msg(3, T(9, 16), "хлеб 4 куска", photo=True))
    assert len(s.purchases) == 4
    assert tg.sent[-1]["text"].startswith("🧾 Покупки сохранил") and "Хлеб" in tg.sent[-1]["text"]

    # «ничего не ел»
    run(bot, clock, T(9, 17, 1), msg(4, T(9, 17), "", photo=True))
    q = tg.sent[-1]
    rid = s.entries[-1]["id"]
    bot.handle_update({"callback_query": {
        "id": "c", "data": f"o:{rid}:0",
        "message": {"message_id": q["id"], "chat": {"id": 42}, "text": q["text"],
                    "reply_markup": {"inline_keyboard": [[{"text": "Ничего не ел",
                                                           "callback_data": f"o:{rid}:0"}]]}}}})
    assert s.entry(rid)["status"] == "чек" and "ничего" in tg.sent[-1]["text"].lower()


def test_edit():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 9, 1), msg(1, T(8, 9), "сырники"))
    assert s.entry(1)["kcal"] == 450
    first_reply = tg.sent[-1]
    assert s.entry(1)["reply_msg_id"] == str(first_reply["id"])
    n = len(tg.sent)
    run(bot, clock, T(8, 9, 5), msg(1, T(8, 9), "овсянка", edited=True))
    old, new = s.entry(1), s.entries[-1]
    assert old["status"] == "изменено" and old["kcal"] == ""
    assert new["item"] == "Овсянка" and new["msg_id"] == 1 and new["time"] == old["time"]
    assert len(tg.sent) == n, "новое сообщение не отправлено — исправлен прежний ответ"
    mid, text = tg.edits[-1]
    assert mid == str(first_reply["id"]) and text.startswith("✏️ Исправлено")
    assert "Калории: 250 / 2945" in text and new["reply_msg_id"] == mid


def test_yesterday_flag():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 13, 1), msg(1, T(8, 13), "сырный суп"))
    run(bot, clock, T(9, 5, 1))
    assert tg.sent[-1]["text"].startswith("🌅")
    run(bot, clock, T(9, 10, 1), msg(2, T(9, 10), "вчера ночью съел чипсы"))
    e = s.entries[-1]
    assert e["item"] == "Чипсы" and e["day"] == "2026-11-08"
    assert "За вс, 8 ноября" in tg.sent[-1]["text"] and "Калории: 800" in tg.sent[-1]["text"]


def test_retro_update():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 12, 1), msg(1, T(8, 12), "гранатовый сок"))
    run(bot, clock, T(9, 5, 1))
    run(bot, clock, T(9, 10, 1), msg(2, T(9, 10), "вчера был он же", photo=True))
    old, new = s.entry(1), s.entries[-1]
    assert old["status"] == "изменено" and old["kcal"] == ""
    assert new["day"] == "2026-11-08" and new["place"] == "Rich" and new["time"] == old["time"]
    assert s.entry(2)["status"] == "ответ", "само фото — не новая еда"
    reply = tg.sent[-1]["text"]
    assert reply.startswith("✏️ Уточнил запись за вс, 8 ноября") and "Калории: 160" in reply, reply
    total = sum(x["kcal"] or 0 for x in s.entries_of("2026-11-08") if x["status"] == "ок")
    assert total == 160, "сок не посчитан дважды"


def test_retro_two_records():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 12, 1), msg(1, T(8, 12), "гранатовый сок"))
    run(bot, clock, T(9, 12, 1), msg(2, T(9, 12), "выпил сок 300 мл"))
    run(bot, clock, T(9, 12, 3), msg(3, T(9, 12, 2), "вчера был он же", photo=True))
    ok = {(x["day"], x["amount"]): x for x in s.entries if x["status"] == "ок"}
    assert ok[("2026-11-08", "250 мл")]["place"] == "Rich"
    assert ok[("2026-11-09", "300 мл")]["place"] == "Rich" and ok[("2026-11-09", "300 мл")]["kcal"] == 190
    assert len(ok) == 2, "ничего не задвоилось"
    assert any(m["text"].startswith("✏️ Уточнил запись за вс, 8 ноября, сегодня") for m in tg.sent)


def test_retro_with_open_question():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 12, 1), msg(1, T(8, 12), "гранатовый сок"))
    run(bot, clock, T(9, 12, 1), msg(2, T(9, 12), "выпил сок 300 мл неполный"))
    assert s.entry(2)["status"] == "ждёт ответа"
    run(bot, clock, T(9, 12, 3), msg(3, T(9, 12, 2), "вчера был он же", photo=True))
    assert s.entry(2)["status"] == "ок", "сегодняшний вопрос закрыт ответом"
    assert s.entry(1)["status"] == "изменено"
    y = [x for x in s.entries if x["day"] == "2026-11-08" and x["status"] == "ок"]
    assert len(y) == 1 and y[0]["place"] == "Rich"


def test_eat_suggestion():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 15, 1), msg(1, T(9, 15), "", photo=True))       # чек → покупки
    run(bot, clock, T(9, 19, 1), msg(2, T(9, 19), "сырный суп"))
    n_entries = len(s.entries)
    run(bot, clock, T(9, 22, 30), msg(3, T(9, 22, 30), "чё бы съесть солёное"))
    assert len(s.entries) == n_entries, "вопрос «че съесть» — не еда"
    ctx = ai.last_suggest
    assert ctx["part_of_day"] == "поздний вечер" and ctx["wish"] == "солёное"
    assert "Калории 2645 ккал" in ctx["left"], ctx["left"]
    assert "Творог" in ctx["purchases"]
    eat = [m for m in tg.sent if m["text"].startswith("🍴")]
    assert eat and eat[-1]["reply_to"] == 3 and ai.last_suggest["mode"] == "eat"
    run(bot, clock, T(9, 22, 31), msg(4, T(9, 22, 31), "/eat"))
    assert ai.last_suggest["wish"] == "нет"


def test_recipe_and_ate_button():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 12, 1), msg(1, T(9, 12), "сырный суп"))
    run(bot, clock, T(9, 19, 1), msg(2, T(9, 19), "хочу что-то с яйцами"))
    ctx = ai.last_suggest
    assert ctx["mode"] == "recipe" and ctx["wish"] == "с яйцами"
    assert "Сырный суп (ВкусВилл) — 1 раз" in ctx["usual"], ctx["usual"]
    r = tg.sent[-1]
    assert r["text"].startswith("👨‍🍳 Рецепты — с яйцами") and "1) 🍳 Омлет" in r["text"]
    assert "Нужно: яйца — 3 шт" in r["text"] and "  1. взбей" in r["text"]
    assert [b[0] for b in r["buttons"][0]] == ["✅ Съел 1", "✅ Съел 2"]
    n = len(s.entries)
    run(bot, clock, T(9, 19, 30), {"callback_query": {"id": "c", "data": "ate:2", "message": {
        "message_id": r["id"], "chat": {"id": 42}, "text": r["text"]}}})
    assert len(s.entries) == n + 1 and s.entries[-1]["item"] == "Яичница с тостом"
    assert "Калории: 550 / 2945" in tg.sent[-1]["text"], tg.sent[-1]["text"]


def test_reply_to_recipe_with_changes():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 9, 1), msg(1, T(9, 9), "хочу что-то с яйцами"))
    r = tg.sent[-1]
    m = msg(2, T(9, 9, 30), "сделал второй, но без тоста и с сыром 30 г")
    m["message"]["reply_to_message"] = {"message_id": r["id"], "text": r["text"], "from": {"is_bot": True}}
    run(bot, clock, T(9, 9, 31), m)
    assert s.entries[-1]["item"].startswith("Яичница (без тоста")
    assert tg.sent[-1]["text"].startswith("🍽")


def test_plan_shop_and_free_request():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 21, 1), msg(1, T(9, 21), "составь меню на завтра"))
    assert ai.last_suggest["mode"] == "plan" and ai.last_suggest["wish"] == "на завтра"
    r = tg.sent[-1]
    assert r["text"].startswith("📋 Меню — на завтра") and "Итого по меню: ≈ 510 ккал" in r["text"], r["text"]
    assert len(r["buttons"]) == 2 and r["buttons"][0][0][0].startswith("✅ ")
    run(bot, clock, T(9, 21, 5), msg(2, T(9, 21, 4), "че купить в ВкусВилл"))
    assert ai.last_suggest["mode"] == "shop" and ai.last_suggest["wish"] == "в ВкусВилл"
    assert tg.sent[-1]["text"].startswith("🛒 Что купить")
    n = len(s.entries)
    run(bot, clock, T(9, 21, 9), msg(3, T(9, 21, 8), "че по белку подскажешь"))
    assert ai.last_suggest["mode"] == "eat" and ai.last_suggest["wish"] == "добрать белок"
    assert tg.sent[-1]["text"].startswith("🍴") and s.entries[-1]["status"] == "не еда"


def test_salt_warning():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 12, 1), msg(1, T(9, 12), "плов из чайзоны"))  # na = 800 из 2000 → 40%
    summary = [m for m in tg.sent if m["text"].startswith("🍽")][-1]["text"]
    assert "⚠️ Плов — 40% дневной нормы (натрий); остаток дня лучше без соли" in summary, summary
    run(bot, clock, T(9, 13, 1), msg(2, T(9, 13), "сырный суп"))       # 300 мг → без предупреждения
    assert "⚠️" not in tg.sent[-1]["text"].split("📊")[0]


def test_day_command():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(9, 9, 1), msg(1, T(9, 9), "сырный суп"))
    run(bot, clock, T(9, 13, 1), msg(2, T(9, 13), "раф из серфа"))
    run(bot, clock, T(9, 18, 15), msg(3, T(9, 18, 15), "/day"))
    r = tg.sent[-1]["text"]
    assert r.startswith("📊 Сегодня, пн, 9 ноября — на 18:15"), r
    assert "Калории: 650 / 2945" in r and "09:00 Сырный суп" in r and "≈" in r
    assert len([e for e in s.entries if e["text"] == "/day"]) == 0


def test_supplements():
    s, tg, ai, bot, clock = make()
    s.norms += [{"key": "mg", "value": 400, "type": "не меньше", "unit": "мг"},
                {"key": "zn", "value": 11, "type": "не меньше", "unit": "мг"}]
    s.supps = [{"slot": "утро", "name": "Цинк", "count": 1, "zn": 25},
               {"slot": "утро", "name": "Омега-3", "count": 1, "kcal": 9, "fat": 1},
               {"slot": "вечер", "name": "Магний B6", "count": 4, "mg": 113},
               {"slot": "вечер", "name": "Омега-3", "count": 1, "kcal": 9, "fat": 1}]
    run(bot, clock, T(9, 8, 1), msg(1, T(9, 8), "выпил утренние бады"))
    r = tg.sent[-1]["text"]
    assert "💊 Отметил бады:\n• утро: Цинк ×1, Омега-3 ×1" in r and "Цинк: 25 / ≥11" in r, r
    run(bot, clock, T(9, 8, 2), msg(2, T(9, 8, 1), "бады утро"))
    assert "Уже отмечено сегодня: утро" in tg.sent[-1]["text"]
    run(bot, clock, T(9, 21, 1), msg(3, T(9, 21), "бады"))
    q = tg.sent[-1]
    assert q["buttons"][0][2] == ("✅ Вечер", "supp:вечер")
    run(bot, clock, T(9, 21, 2), {"callback_query": {"id": "c", "data": "supp:вечер", "message": {
        "message_id": q["id"], "chat": {"id": 42}, "text": q["text"]}}})
    assert "Магний: 452 / ≥400" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    sup = [e for e in s.entries if e["kind"] == "бад"]
    assert len(sup) == 2 and sum(e["kcal"] for e in sup) == 18
    assert "Бады" not in bot.history_text() and "Бады" not in bot.usual_text()


def test_store_helpers():
    assert to_cell("2026-11-08") == "'2026-11-08"
    assert to_cell(5) == 5 and to_cell(None) == ""
    f = day_formulas(5)
    assert f["kcal"] == "=SUMIFS('Записи'!K:K,'Записи'!$B:$B,$A5)"
    f = day_formulas(5, ";")
    assert f["kcal"] == "=SUMIFS('Записи'!K:K;'Записи'!$B:$B;$A5)"
    assert "X:$X" in f["approx"]
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот: {"a": 2} готово') == {"a": 2}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)


def test_supp_training_slot():
    from bot.logic import supp_slots
    assert supp_slots("креатин перед тренировкой") == ["тренировка"]
    assert supp_slots("бады утро") == ["утро"]
    assert supp_slots("бады все") == ["утро", "день", "вечер"]
    assert supp_slots("креатин") == []


def test_late_caffeine_and_supp_omega():
    s, tg, ai, bot, clock = make()
    s.norms += [{"key": "omega3", "value": 250, "type": "не меньше", "unit": "мг"}]
    s.supps = [{"slot": "утро", "name": "Омега-3", "count": 1, "omega3": 350, "creatine": 0}]
    run(bot, clock, T(9, 8, 1), msg(1, T(9, 8), "бады утро"))
    assert "Омега-3 (ЭПК+ДГК): 350 / ≥250" in tg.sent[-1]["text"], tg.sent[-1]["text"]
    late = [{"item": "Кола", "caffeine": 48, "time": "2026-11-09 19:00"},
            {"item": "Американо", "caffeine": 160, "time": "2026-11-09 10:00"}]
    w = bot.limit_warnings(late, "2026-11-09")
    assert len(w) == 1 and w[0].startswith("☕ Кола — ≈48 мг кофеина после 17:00"), w


def test_same_as_copies_and_busy_notice():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 20), msg(1, T(8, 20), "пельмени 400"))
    run(bot, clock, T(9, 14), msg(2, T(9, 14), "пельмени как вчера"))
    pel = [e for e in s.entries if e["item"] == "Пельмени"]
    assert [e["kcal"] for e in pel] == [800, 800] and pel[1]["source"] == "как раньше"
    run(bot, clock, T(9, 15), msg(3, T(9, 15), "boom"))
    assert "перегружен" in tg.sent[-1]["text"]
    n = len(tg.sent)
    bot.process_new()
    assert len(tg.sent) == n   # второй раз не пишем


def test_day_arg_and_past_report():
    from bot.logic import parse_day_arg
    t = "2026-11-17"  # вторник
    assert parse_day_arg("", t) == t
    assert parse_day_arg("вчера", t) == "2026-11-16"
    assert parse_day_arg("позавчера", t) == "2026-11-15"
    assert parse_day_arg("15.11", t) == "2026-11-15"
    assert parse_day_arg("9 ноября", t) == "2026-11-09"
    assert parse_day_arg("понедельник", t) == "2026-11-16"
    assert parse_day_arg("вт", t) == t
    assert parse_day_arg("31.12", t) == "2025-12-31"
    assert parse_day_arg("ерунда", t) is None
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 20), msg(1, T(8, 20), "пельмени 400"))
    run(bot, clock, T(9, 14), msg(2, T(9, 14), "статистика за вчера"))
    r = [x["text"] for x in tg.sent if x["text"].startswith("📊 За")][-1]
    assert r.startswith("📊 За вс, 8 ноября") and "Пельмени" in r, r
    run(bot, clock, T(9, 14, 1), msg(3, T(9, 14, 1), "/day 1.11"))
    assert "записей нет" in tg.sent[-1]["text"]


def test_choose_fallback():
    from bot.gemini import choose_fallback
    names = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash-lite", "gemini-3.6-flash-image",
             "gemini-3.6-pro", "gemini-embedding-001", "gemini-3.6-flash-tts"]
    assert choose_fallback(names, "gemini-3.6-flash") == "gemini-3.6-flash-lite"
    assert choose_fallback(["gemini-3.6-flash", "gemini-3.5-flash"], "gemini-3.6-flash") == "gemini-3.5-flash"
    assert choose_fallback(["gemini-3.6-flash"], "gemini-3.6-flash") == ""


def test_gemini_search_quota_falls_back_to_no_search():
    from bot.gemini import Gemini

    class R:
        def __init__(self, code, text="", body=None):
            self.status_code, self.text, self._b = code, text, body

        def json(self):
            return self._b

    g = Gemini("k", "main-model")
    g._auto_fallback = "lite-model"
    calls = []

    def post(parts, search, json_mode, system=None, model=None):
        calls.append((model, search))
        if search:
            return R(429, '{"error": "You exceeded your current quota"}')
        return R(200, body={"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]})

    g._post = post
    assert g._call([{"text": "x"}], use_search=True) == {"ok": 1}
    assert calls == [("main-model", True), ("lite-model", True), ("main-model", False)]
    assert g.no_search
