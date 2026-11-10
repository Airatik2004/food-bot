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

    def edit_text(self, *a, **k):
        pass

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
        if "творог" in text:
            assert "Творог с малиной" in purchases
            return {"transcript": text, "items": [item("Творог с малиной 5%", 170, place="Верейский")]}
        if "плов" in text:
            return {"transcript": text, "items": [item("Плов", 800, complete=False,
                                                       place="Чайхона №1")]}
        if "гранатовый сок" in text:
            return {"transcript": text, "items": [item("Гранатовый сок", 150, place="", amount="250 мл")]}
        if "вчера был он же" in text:
            sid = int([l for l in history.split("\n") if "Гранатовый" in l][0].split(" | ")[0])
            return {"transcript": "бутылка сока Rich", "items": [
                item("Гранатовый сок", 160, place="Rich", amount="250 мл", updates_id=sid)]}
        if "вчера" in text:
            return {"transcript": text, "items": [item("Чипсы", 500, place="Lay's", yesterday=True)]}
        if "карамель" in text and open_q:
            first_id = int(open_q.split(" | ")[0])
            return {"transcript": text, "answer_to": first_id, "items": []}
        return {"transcript": text, "items": []}

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
    assert "Итого за сегодня: 300 / 2945 ккал" in r["text"], r["text"]

    run(bot, clock, T(8, 13, 1), msg(2, T(8, 13), "раф из серфа"))
    summary, q = tg.sent[-2], tg.sent[-1]
    assert "~350" in summary["text"] and "уточню ниже" in summary["text"]
    assert q["text"].startswith("❓") and q["reply_to"] == 2
    assert q["buttons"][0][0][0] == "Классический 400 мл"
    raf = s.entry(2)
    assert raf["status"] == "ждёт ответа" and raf["options"]

    n = len(tg.sent)
    run(bot, clock, T(8, 15))
    assert len(tg.sent) == n, "днём без новых сообщений бот молчит"

    # 21:01 — повтор неотвеченных вопросов
    run(bot, clock, T(8, 21, 1))
    assert tg.sent[-2]["text"].startswith("🕘") and tg.sent[-1]["text"].startswith("❓")
    assert tg.sent[-1]["buttons"][0][0][0] == "Классический 400 мл", "варианты берутся из таблицы"
    q2 = tg.sent[-1]

    run(bot, clock, T(8, 21, 31))
    assert "⏳" in tg.sent[-1]["text"]
    n = len(tg.sent)
    run(bot, clock, T(8, 21, 36))
    assert len(tg.sent) == n, "не спамит напоминаниями"

    # кнопка на повторном вопросе
    bot.handle_update({"callback_query": {
        "id": "c1", "data": f"o:{raf['id']}:1",
        "message": {"message_id": q2["id"], "chat": {"id": 42}, "text": q2["text"],
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "Классический 400 мл", "callback_data": f"o:{raf['id']}:0"}],
                        [{"text": "Солёная карамель 400 мл", "callback_data": f"o:{raf['id']}:1"}]]}}}})
    assert raf["status"] == "ок" and raf["kcal"] == 380
    assert tg.sent[-1]["text"].startswith("✅") and "680" in tg.sent[-1]["text"]

    run(bot, clock, T(8, 21, 40))
    rep = tg.sent[-1]["text"]
    assert rep.startswith("📊 Отчёт за вс, 8 ноября") and "680" in rep, rep

    # ночной перекус → тот же день, поправка после 05:00
    run(bot, clock, T(9, 0, 31), msg(3, T(9, 0, 30), "сырники"))
    assert s.entry(3)["day"] == "2026-11-08"
    assert "Итого за сегодня: 1130" in tg.sent[-1]["text"], "до 05:00 это ещё тот же день"
    n = len(tg.sent)
    run(bot, clock, T(9, 1, 1))
    assert len(tg.sent) == n
    run(bot, clock, T(9, 8, 1))
    corr = tg.sent[-1]["text"]
    assert corr.startswith("✏️ Поправка") and "Сырники" in corr and "1130" in corr, corr
    assert s.day("2026-11-08")["phase"] == "закрыт"


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
    assert "Итого за сегодня: 170" in tg.sent[-1]["text"]

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
    run(bot, clock, T(8, 9, 5), msg(1, T(8, 9), "овсянка", edited=True))
    old, new = s.entry(1), s.entries[-1]
    assert old["status"] == "изменено" and old["kcal"] == ""
    assert new["item"] == "Овсянка" and new["msg_id"] == 1 and new["time"] == old["time"]
    assert tg.sent[-1]["text"].startswith("✏️ Исправил") and "Итого за сегодня: 250" in tg.sent[-1]["text"]


def test_yesterday_flag():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 13, 1), msg(1, T(8, 13), "сырный суп"))
    run(bot, clock, T(8, 21, 31))
    assert tg.sent[-1]["text"].startswith("📊")
    run(bot, clock, T(9, 10, 1), msg(2, T(9, 10), "вчера ночью съел чипсы"))
    e = s.entries[-1]
    assert e["item"] == "Чипсы" and e["day"] == "2026-11-08"
    assert any("Поправка" in m["text"] and "Чипсы" in m["text"] for m in tg.sent)


def test_retro_update():
    s, tg, ai, bot, clock = make()
    run(bot, clock, T(8, 12, 1), msg(1, T(8, 12), "гранатовый сок"))
    run(bot, clock, T(8, 21, 31))
    assert tg.sent[-1]["text"].startswith("📊") and "150" in tg.sent[-1]["text"]
    run(bot, clock, T(9, 10, 1), msg(2, T(9, 10), "вчера был он же", photo=True))
    old, new = s.entry(1), s.entries[-1]
    assert old["status"] == "изменено" and old["kcal"] == ""
    assert new["day"] == "2026-11-08" and new["place"] == "Rich" and new["time"] == old["time"]
    assert s.entry(2)["status"] == "ответ", "само фото — не новая еда"
    reply = [m for m in tg.sent if m["text"].startswith("✏️ Уточнил")][0]["text"]
    assert "вс, 8 ноября" in reply and "Итого за вс, 8 ноября: 160" in reply, reply
    corr = [m for m in tg.sent if m["text"].startswith("✏️ Поправка")]
    assert corr and "Rich" in corr[-1]["text"], "поправка к уже отправленному отчёту"
    total = sum(x["kcal"] or 0 for x in s.entries_of("2026-11-08") if x["status"] == "ок")
    assert total == 160, "сок не посчитан дважды"


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
