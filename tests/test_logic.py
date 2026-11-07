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
        return b"audio"


class FakeAI:
    def __init__(self):
        self.calls = 0

    def analyze(self, t, text, media, dct, open_q):
        self.calls += 1
        if "суп" in text:
            return {"transcript": text, "items": [item("Сырный суп", 300)]}
        if "раф" in text:
            return {"transcript": text, "items": [item("Раф", 350, complete=False, place="Surf Coffee")]}
        if "сырники" in text:
            return {"transcript": text, "items": [item("Сырники", 450, place="дом")]}
        if "овсянка" in text:
            return {"transcript": text, "items": [item("Овсянка", 250, place="дом")]}
        if "плов" in text:
            return {"transcript": text, "items": [item("Плов", 800, complete=False,
                                                       place="Чайхона №1")]}
        if "вчера" in text:
            return {"transcript": text, "items": [item("Чипсы", 500, place="Lay's", yesterday=True)]}
        if "карамель" in text and open_q:
            first_id = int(open_q.split(" | ")[0])
            return {"transcript": text, "answer_to": first_id, "items": []}
        return {"transcript": text, "items": []}

    def refine(self, text, it, hist, answer, media, dct):
        self.calls += 1
        return {"answer_text": answer,
                "item": item("Раф", 380, place="Surf Coffee", variant="солёная карамель",
                             amount="400 мл", grams=400)}


def msg(mid, dt, text, reply_to=None):
    m = {"message_id": mid, "date": int(dt.timestamp()), "chat": {"id": 42}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"message": m}


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


def test_assign_day():
    ds = hhmm("05:00")
    assert assign_day(T(8, 9), "", ds) == "2026-11-08"
    assert assign_day(T(9, 0, 30), "2026-11-08", ds) == "2026-11-08"   # ночь → вчера
    assert assign_day(T(9, 4, 59), "2026-11-08", ds) == "2026-11-08"
    assert assign_day(T(9, 9, 30), "2026-11-08", ds) == "2026-11-09"   # утро → новый день
    assert assign_day(T(9, 3), "2026-11-09", ds) == "2026-11-09"       # после /newday
    assert hhmm(0.2083333) == hhmm("05:00")


def test_full_day():
    s, tg, ai, bot, clock = make()
    # днём пишем
    bot.handle_update(msg(1, T(8, 9), "сырный суп из вкусвилла"))
    bot.handle_update(msg(2, T(8, 13), "раф из серфа"))
    clock["now"] = T(8, 15)
    bot.tick()
    assert tg.sent == [] and ai.calls == 0, "днём бот молчит и не тратит запросы"

    # 21:01 — вопросы
    clock["now"] = T(8, 21, 1)
    bot.tick()
    assert s.day("2026-11-08")["phase"] == "уточнение"
    q = [m for m in tg.sent if m["text"].startswith("❓")]
    assert len(q) == 1 and "Раф" in q[0]["text"] and q[0]["reply_to"] == 2
    assert q[0]["buttons"][0][0][0] == "Классический 400 мл"
    raf = s.entry(2)
    assert raf["status"] == "ждёт ответа"

    # 21:31 без ответа — напоминание, а не отчёт
    clock["now"] = T(8, 21, 31)
    bot.tick()
    assert "⏳" in tg.sent[-1]["text"]
    assert not any(m["text"].startswith("📊") for m in tg.sent)
    # повторный запуск через 5 минут не спамит
    clock["now"] = T(8, 21, 36)
    n = len(tg.sent)
    bot.tick()
    assert len(tg.sent) == n

    # нажимаем кнопку
    bot.handle_update({"callback_query": {
        "id": "c1", "data": f"o:{raf['id']}:1",
        "message": {"message_id": q[0]["id"], "chat": {"id": 42}, "text": q[0]["text"],
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "Классический 400 мл", "callback_data": f"o:{raf['id']}:0"}],
                        [{"text": "Солёная карамель 400 мл", "callback_data": f"o:{raf['id']}:1"}]]}}}})
    assert raf["status"] == "ок" and raf["kcal"] == 380
    assert "Солёная карамель" in raf["answers"]
    assert tg.sent[-1]["text"].startswith("✅")
    assert any(r["place"] == "Surf Coffee" and r["variant"] == "солёная карамель" for r in s.dictionary)

    clock["now"] = T(8, 21, 40)
    bot.tick()
    rep = tg.sent[-1]["text"]
    assert rep.startswith("📊 Отчёт за вс, 8 ноября"), rep
    assert "680" in rep  # 300 + 380 ккал
    assert "Натрий" in rep
    assert s.day("2026-11-08")["phase"] == "отчёт"

    # ночной перекус после отчёта → тот же день
    bot.handle_update(msg(3, T(9, 0, 30), "сырники"))
    assert s.entry(3)["day"] == "2026-11-08"
    clock["now"] = T(9, 1, 1)
    n = len(tg.sent)
    bot.tick()
    assert len(tg.sent) == n, "до утра поправку не шлём"

    # утро — новый день, поправка за вчера
    bot.handle_update(msg(4, T(9, 9, 30), "овсянка"))
    assert s.entry(4)["day"] == "2026-11-09"
    clock["now"] = T(9, 11, 1)
    bot.tick()
    corr = tg.sent[-1]["text"]
    assert corr.startswith("✏️ Поправка") and "Сырники" in corr and "1130" in corr, corr
    assert s.day("2026-11-08")["phase"] == "закрыт"
    assert s.entry(4)["status"] == "новая", "сегодняшнее разберём только вечером"


def test_deadline_and_answer_by_text():
    s, tg, ai, bot, clock = make()
    s.state["current_day"] = "2026-11-08"
    bot.handle_update(msg(1, T(8, 14), "плов из чайзоны"))
    bot.handle_update(msg(2, T(8, 15), "раф из серфа"))
    clock["now"] = T(8, 21, 2)
    bot.tick()
    qs = [m for m in tg.sent if m["text"].startswith("❓")]
    assert len(qs) == 2
    # ответ обычным сообщением (не реплаем) → ИИ понял, что это ответ
    bot.handle_update(msg(3, T(8, 21, 10), "солёная карамель 400"))
    bot.tick()
    assert s.entry(3)["status"] == "ответ"
    assert s.entry(1)["status"] == "ок"   # заглушка относит ответ к первому вопросу
    # дедлайн
    clock["now"] = T(9, 1, 2)
    bot.tick()
    rep = tg.sent[-1]["text"]
    assert rep.startswith("📊") and "≈" in rep, rep
    assert s.entry(2)["status"] == "примерно"


def test_reply_and_yesterday():
    s, tg, ai, bot, clock = make()
    bot.handle_update(msg(1, T(8, 13), "раф"))
    clock["now"] = T(8, 21, 1)
    bot.tick()
    q = [m for m in tg.sent if m["text"].startswith("❓")][0]
    bot.handle_update(msg(2, T(8, 21, 5), "солёная карамель 400 мл", reply_to=q["id"]))
    assert s.entry(1)["status"] == "ок"
    clock["now"] = T(8, 21, 31)
    bot.tick()
    assert tg.sent[-1]["text"].startswith("📊")
    clock["now"] = T(9, 2)
    bot.tick()
    # день закрывается утром после нового сообщения
    bot.handle_update(msg(3, T(9, 10), "вчера ночью съел чипсы"))
    clock["now"] = T(9, 21, 1)
    bot.tick()
    e = s.entries[-1]
    assert e["item"] == "Чипсы"
    assert e["day"] == "2026-11-08", e["day"]
    assert any("Поправка" in m["text"] and "Чипсы" in m["text"] for m in tg.sent)


def test_store_helpers():
    assert to_cell("2026-11-08") == "'2026-11-08"
    assert to_cell(5) == 5 and to_cell(None) == ""
    f = day_formulas(5)
    assert f["kcal"] == "=SUMIFS('Записи'!K:K,'Записи'!$B:$B,$A5)"
    assert f["p"].startswith("=SUMIFS('Записи'!W:W")
    assert "X:$X" in f["approx"]
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот: {"a": 2} готово') == {"a": 2}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
