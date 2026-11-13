"""Логика бота: разбор записей, вопросы, дни, отчёты."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .schema import (ANSWER, APPROX, ASKING, CHANGED, CLOSED, COLLECT, FOOD_DONE, NEED_ASK,
                     NEW, NOT_FOOD, NUTRIENT_NAMES, NUTRIENTS, OK, OPEN, RECEIPT, REPORTED,
                     WAITING)
from .store import Store, to_num

log = logging.getLogger("logic")
MSK = ZoneInfo("Europe/Moscow")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
COUNTED = FOOD_DONE + (NEED_ASK, WAITING)   # что входит в «итого за день» (с оценкой)
RECEIPT_SRC = "чек"

HELP = """Привет! Я записываю, что ты ешь, и считаю КБЖУ.

• Пиши или говори голосом: «сырный суп из ВкусВилла», «раф из Surf 400 мл». Можно фото блюда или этикетки.
• На каждую запись отвечаю разбором и калориями. Если чего-то не хватает (граммы, место, вкус), сразу спрошу — отвечай кнопкой или реплаем. Можно «как вчера».
• Чек: пришли фото — сохраню покупки и спрошу, что из этого ты съел. В подписи можно сразу написать: «съел хлеб, 4 куска».
• Ошибся — просто отредактируй сообщение, я пересчитаю.
• В {ask} доспрошу неотвеченное, отчёт — в {report}, когда всё уточнено. Напоминаю до {deadline}, потом считаю «примерно».
• Новый день начинается в {start}: всё, что съел ночью, идёт во вчера.

Команды:
/eat — что съесть сейчас (с учётом остатка КБЖУ и времени). Можно и текстом: «че съесть», «что бы съесть сладкое»
/report — промежуточный отчёт за сегодня
/status — что ждёт уточнения"""

EAT_RE = re.compile(r"^\s*(?:/eat\b|(?:ч[её]|что|чо|шо)\s+(?:бы\s+)?(?:мне\s+)?"
                    r"(?:с[ъь]?есть|поесть|покушать|пожрать)\b)[\s?!.,]*(.*)$", re.I | re.S)


def hhmm(s, default="00:00") -> time:
    s = str(s or default).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if not m:
        # Таблица может вернуть время как долю суток (0.2083 = 05:00)
        f = to_num(s)
        if isinstance(f, (int, float)) and 0 <= f < 1:
            mins = round(f * 24 * 60)
            return time(mins // 60, mins % 60)
        return hhmm(default)
    return time(int(m.group(1)), int(m.group(2)))


def d_(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def at(day: str, t: time) -> datetime:
    return datetime.combine(d_(day), t, MSK)


def fmt_day(day: str) -> str:
    d = d_(day)
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def assign_day(ts: datetime, day_start: time) -> str:
    """Логический день: сутки начинаются в day_start (по умолчанию 05:00)."""
    t = ts.astimezone(MSK)
    d = t.date() if t.time() >= day_start else t.date() - timedelta(days=1)
    return d.isoformat()


def fmt_num(v, unit):
    v = to_num(v, 0) or 0
    if not v:
        return "0"
    if unit == "ккал" or unit == "мг" or abs(v) >= 10:
        return f"{round(v):d}"
    return f"{v:.1f}".replace(".", ",")


class FoodBot:
    def __init__(self, store: Store, tg, gemini, chat_id, now_fn=None):
        self.s = store
        self.tg = tg
        self.ai = gemini
        self.chat_id = str(chat_id) if chat_id else ""
        self.now_fn = now_fn or (lambda: datetime.now(MSK))
        self._reopened = False
        self.edited_ids: set = set()
        self.activity = False       # пользователь что-то прислал в этом запуске
        self.asked = False          # бот задал вопросы в этом запуске

    # ---------------- настройки ----------------
    def t(self, key) -> time:
        return hhmm(self.s.setting(key))

    @property
    def tol(self) -> float:
        return float(to_num(self.s.setting("tolerance"), 10)) / 100

    def now(self) -> datetime:
        return self.now_fn()

    def stamp(self, dt=None) -> str:
        return (dt or self.now()).astimezone(MSK).strftime("%Y-%m-%d %H:%M")

    def today(self) -> str:
        return assign_day(self.now(), self.t("day_start"))

    def send(self, text, **kw):
        return self.tg.send(self.chat_id, text, **kw)

    # ---------------- входящие ----------------
    def handle_update(self, u: dict):
        if "callback_query" in u:
            cq = u["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                return
            self.activity = True
            self.handle_callback(cq)
            return
        m = u.get("message") or u.get("edited_message")
        if not m:
            return
        chat = str(m.get("chat", {}).get("id"))
        if not self.chat_id:
            self.tg.send(chat, f"Твой chat id: {chat}\nДобавь его в секрет TELEGRAM_CHAT_ID на GitHub.")
            return
        if chat != self.chat_id:
            log.info("Сообщение из чужого чата %s — пропускаю", chat)
            return
        self.activity = True
        if "edited_message" in u:
            self.handle_edit(m)
        else:
            self.handle_message(m)

    @staticmethod
    def parse_media(m):
        if m.get("voice"):
            return "голос", m["voice"]["file_id"]
        if m.get("audio"):
            return "аудио", m["audio"]["file_id"]
        if m.get("photo"):
            return "фото", m["photo"][-1]["file_id"]
        if m.get("document") and str(m["document"].get("mime_type", "")).startswith("image/"):
            return "фото", m["document"]["file_id"]
        return "текст", ""

    def handle_message(self, m: dict):
        text = (m.get("text") or m.get("caption") or "").strip()
        eat = EAT_RE.match(text) if not m.get("reply_to_message") else None
        if eat:
            return self.suggest(eat.group(1).strip(), reply_to=m["message_id"])
        if text.startswith("/"):
            return self.command(text.split()[0].split("@")[0].lower())
        ts = datetime.fromtimestamp(m["date"], MSK)
        kind, file_id = self.parse_media(m)
        if kind == "текст" and not text:
            self.send("Я понимаю текст, голосовые и фото 🙂", reply_to=m["message_id"])
            return

        # Ответ на вопрос бота?
        target = None
        rt = m.get("reply_to_message")
        if rt:
            target = self.entry_by_question(rt["message_id"])
        if not target and self.s.state.get("awaiting"):
            target = self.s.entry(self.s.state["awaiting"])
            if target and target["status"] != WAITING:
                target = None
        if target and target.get("source") == RECEIPT_SRC:
            self.receipt_answer(target, text, kind, file_id, m["message_id"])
            return
        if target:
            self.apply_answer(target, text, self.media(kind, file_id), reply_to=m["message_id"])
            return

        # Альбом: несколько фото одним сообщением (например, длинный чек)
        group = str(m.get("media_group_id") or "")
        if group:
            for e in self.s.entries:
                if e.get("media_group") == group and e["status"] == NEW:
                    e["file_id"] = ",".join(x for x in [e["file_id"], file_id] if x)
                    if text:
                        e["text"] = (e["text"] + " " + text).strip()
                    self.s.touch(e)
                    return

        day = assign_day(ts, self.t("day_start"))
        self.s.day(day)  # создать строку дня, если её нет
        if day > str(self.s.state.get("current_day") or ""):
            self.s.set_state("current_day", day)
        self.s.add_entry(day=day, time=self.stamp(ts), kind=kind, text=text, status=NEW,
                         msg_id=m["message_id"], file_id=file_id, media_group=group)

    def handle_edit(self, m: dict):
        """Отредактированное сообщение: старые строки помечаем «изменено», запись разбираем заново."""
        mid = m["message_id"]
        text = (m.get("text") or m.get("caption") or "").strip()
        rows = [e for e in self.s.entries if str(e.get("msg_id")) == str(mid)
                and e["status"] not in (CHANGED, ANSWER)]
        if not rows:
            return  # правка ответа на вопрос или чужого сообщения — игнорируем
        first = rows[0]
        if all(e["status"] == NEW for e in rows):
            first["text"] = text
            self.s.touch(first)
            return
        for e in rows:
            e["status"] = CHANGED
            for k in NUTRIENTS:
                e[k] = ""
            self.s.touch(e)
            if str(self.s.state.get("awaiting")) == str(e["id"]):
                self.s.set_state("awaiting", "")
        kind, file_id = self.parse_media(m)
        new = self.s.add_entry(day=first["day"], time=first["time"], kind=first["kind"],
                               text=text, status=NEW, msg_id=mid,
                               file_id=first.get("file_id") or file_id,
                               media_group=first.get("media_group", ""))
        self.edited_ids.add(new["id"])
        d = self.s.day(first["day"])
        if d["phase"] == CLOSED:
            d["phase"] = REPORTED
            self.s.touch_day(d)

    def handle_callback(self, cq: dict):
        data = cq.get("data", "")
        msg = cq.get("message", {})
        parts = data.split(":")
        e = self.s.entry(parts[1]) if len(parts) > 1 else None
        if not e or e["status"] != WAITING:
            self.tg.answer_callback(cq["id"], "Уже учтено")
            return
        if parts[0] == "x":
            self.tg.answer_callback(cq["id"])
            r = self.send("Напиши или скажи голосом, что именно — ответом на это сообщение.",
                          reply_to=msg.get("message_id"))
            self.add_question_id(e, r["message_id"])
            self.s.set_state("awaiting", e["id"])
            return
        label = ""
        for row in msg.get("reply_markup", {}).get("inline_keyboard", []):
            for b in row:
                if b.get("callback_data") == data:
                    label = b.get("text", "")
        self.tg.answer_callback(cq["id"], label or None)
        self.tg.edit_text(self.chat_id, msg.get("message_id"),
                          (msg.get("text") or "") + f"\n✔️ {label}")
        self.apply_answer(e, label, [], reply_to=None)

    def command(self, cmd):
        if cmd == "/start" and hasattr(self.tg, "call"):
            try:  # меню команд в Telegram
                self.tg.call("setMyCommands", commands=[
                    {"command": "eat", "description": "Что съесть сейчас"},
                    {"command": "report", "description": "Промежуточный отчёт за сегодня"},
                    {"command": "status", "description": "Что ждёт уточнения"},
                    {"command": "help", "description": "Как пользоваться"}])
            except Exception as ex:
                log.info("setMyCommands: %s", ex)
        if cmd in ("/start", "/help"):
            self.send(HELP.format(ask=self.s.setting("ask_time"), report=self.s.setting("report_time"),
                                  deadline=self.s.setting("deadline"), start=self.s.setting("day_start")))
        elif cmd == "/report":
            self.process_new()
            self.send(self.report_text(self.today(), preview=True))
        elif cmd == "/status":
            open_ = [e for e in self.s.entries if e["status"] in OPEN]
            if not open_:
                self.send("Всё уточнено 👌")
            else:
                lines = [f"• {e['day']}: {self.label(e) or e['text'][:40]} — {e['status']}" for e in open_]
                self.send("Ждут разбора или ответа:\n" + "\n".join(lines))
        elif cmd == "/newday":
            self.send(f"Теперь новый день начинается сам в {self.s.setting('day_start')}.")
        else:
            self.send("Не знаю такой команды. /help")

    # ---------------- «че съесть» ----------------
    def suggest(self, wish="", reply_to=None):
        self.process_new()  # сначала учесть то, что ещё не разобрано
        day = self.today()
        food = [e for e in self.s.entries_of(day) if e["status"] in COUNTED]
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        left, over, low = [], [], []
        for n in self.s.norms:
            key = n["key"]
            if key not in NUTRIENTS or not n["value"]:
                continue
            name, unit = NUTRIENT_NAMES[key]
            val, norm, typ = totals[key], float(n["value"]), str(n["type"]).lower()
            if "больше" in typ:
                if val > norm:
                    over.append(f"{name} {fmt_num(val, unit)}/{fmt_num(norm, unit)} {unit}")
            elif key in ("kcal", "protein", "fat", "carbs"):
                diff = norm - val
                (left if diff > 0 else over).append(f"{name} {fmt_num(abs(diff), unit)} {unit}")
            elif val < norm * (1 - self.tol):
                low.append(f"{name} {fmt_num(val, unit)}/{fmt_num(norm, unit)} {unit}")
        now = self.now()
        h = now.hour
        part = ("ночь" if h < 5 else "утро" if h < 11 else "день" if h < 17
                else "вечер" if h < 22 else "поздний вечер")
        usual = sorted({f"{r['place']}: {r['item']} {r['variant']} {r['portion']}".strip()
                        for r in self.s.dictionary})[-40:]
        try:
            text = self.ai.suggest(
                time=now.strftime("%H:%M"), part_of_day=part, day_end=self.s.setting("day_start"),
                eaten=", ".join(f"{fmt_num(totals[k], NUTRIENT_NAMES[k][1])} {n}" for k, n in
                                (("kcal", "ккал"), ("protein", "г белка"), ("fat", "г жиров"),
                                 ("carbs", "г углеводов"))),
                left=", ".join(left) or "норма закрыта", over=", ".join(over) or "нет",
                low=", ".join(low) or "нет", purchases=self.purchases_text(7) or "(нет)",
                usual="\n".join(usual) or self.history_text() or "(пока мало данных)",
                wish=wish or "нет")
        except Exception as ex:
            log.exception("suggest: %s", ex)
            self.send("Не получилось придумать, попробуй ещё раз через минуту.", reply_to=reply_to)
            return
        self.send(f"🍴 {text}", reply_to=reply_to)

    # ---------------- вопросы ----------------
    def entry_by_question(self, msg_id):
        for e in self.s.entries:
            ids = [x for x in str(e.get("question_msg_id") or "").split(",") if x]
            if str(msg_id) in ids and e["status"] == WAITING:
                return e
        return None

    def add_question_id(self, e, msg_id):
        ids = [x for x in str(e.get("question_msg_id") or "").split(",") if x]
        ids.append(str(msg_id))
        e["question_msg_id"] = ",".join(ids)
        self.s.touch(e)

    def ask(self, e, prefix=""):
        opts = [o for o in str(e.get("options") or "").split(" | ") if o]
        buttons = [[(o, f"o:{e['id']}:{k}")] for k, o in enumerate(opts)]
        buttons.append([("Другой / свой ответ ✏️", f"x:{e['id']}")])
        head = "🧾" if e.get("source") == RECEIPT_SRC else "❓"
        r = self.send(f"{prefix}{head} {self.label(e)}\n{e['question']}",
                      reply_to=e["msg_id"] or None, buttons=buttons)
        self.add_question_id(e, r["message_id"])
        e["status"] = WAITING
        self.asked = True

    def send_questions(self, day=None):
        for e in self.s.entries:
            if e["status"] == NEED_ASK and (day is None or e["day"] == day):
                self.ask(e)

    def open_questions_text(self):
        return "\n".join(f"{e['id']} | {self.label(e)} | {e['question']}"
                         for e in self.s.entries if e["status"] == WAITING)

    # ---------------- контекст для ИИ ----------------
    def dictionary_text(self):
        rows = self.s.dictionary[-400:]
        return "\n".join(f"{r['_row']} | {r['place']} | {r['item']} | {r['variant']} | "
                         f"{r['portion']} | {r['kcal']}" for r in rows)

    def history_text(self, days=4):
        since = (d_(self.today()) - timedelta(days=days)).isoformat()
        rows = [e for e in self.s.entries
                if e["status"] in FOOD_DONE + (NEED_ASK, WAITING) and e["day"] >= since]
        return "\n".join(f"{e['id']} | {e['time']} | {e['item']} | {e['place']} | {e['variant']} | "
                          f"{e['amount']} | {fmt_num(e['kcal'], 'ккал')}"
                          + (" | ждёт уточнения" if e["status"] in (NEED_ASK, WAITING) else "")
                          for e in rows[-60:])

    def purchases_text(self, days=14):
        since = (d_(self.today()) - timedelta(days=days)).isoformat()
        rows = [p for p in self.s.purchases if str(p.get("date", "")) >= since]
        return "\n".join(f"{p['date']} | {p['name']} | {p['brand']} | {p['amount']}"
                         for p in rows[-80:])

    def media(self, kind, file_ids):
        out = []
        mime = {"голос": "audio/ogg", "аудио": "audio/mpeg", "фото": "image/jpeg"}.get(kind)
        for fid in [x for x in str(file_ids or "").split(",") if x]:
            try:
                out.append((self.tg.download(fid), mime))
            except Exception as ex:
                log.warning("Не скачал файл: %s", ex)
        return out

    # ---------------- разбор ----------------
    def process_new(self):
        """Разобрать все новые записи и сразу ответить на каждую."""
        for e in sorted([x for x in self.s.entries if x["status"] == NEW],
                        key=lambda x: to_num(x["id"], 0)):
            try:
                self.analyze_entry(e)
            except Exception as ex:
                log.exception("Ошибка разбора записи %s: %s", e["id"], ex)

    def analyze_entry(self, e):
        media = self.media(e["kind"], e.get("file_id"))
        caption = e["text"]
        res = self.ai.analyze(e["time"][11:16], caption, media, self.dictionary_text(),
                              self.open_questions_text(), self.history_text(),
                              self.purchases_text())
        tr = str(res.get("transcript") or "").strip()
        if e["kind"] in ("голос", "аудио") and tr:
            e["text"] = "🎤 " + tr
        elif e["kind"] == "фото" and tr:
            e["text"] = (caption + " " if caption else "") + f"[фото: {tr}]"

        target = self.s.entry(res.get("answer_to")) if res.get("answer_to") else None
        if target and target["status"] == WAITING:
            e["status"] = ANSWER
            self.s.touch(e)
            self.apply_answer(target, tr or caption, [], reply_to=e["msg_id"])
            # то же сообщение может уточнять и другие записи («вчера был он же»)
            res["items"] = [it for it in (res.get("items") or [])
                            if self.retro_target(it, e) and self.s.entry(it["updates_id"]) is not target]
            res["receipt"] = []
            if not res["items"]:
                return

        receipt = res.get("receipt") or []
        for p in receipt:
            self.s.add_purchase({"date": e["day"], **p})
        items = res.get("items") or []
        edited = e["id"] in self.edited_ids

        if not items:
            if receipt:
                e.update(item="Чек", source=RECEIPT_SRC, status=NEED_ASK, options="Ничего не ел",
                         question=f"Сохранил покупки ({len(receipt)}): "
                                  + ", ".join(p["name"] for p in receipt[:12])
                                  + ("…" if len(receipt) > 12 else "")
                                  + "\nЧто из этого ты уже съел и сколько? Если ничего — нажми кнопку.")
                self.s.touch(e)
                self.ask(e)
            else:
                e["status"] = NOT_FOOD
                self.s.touch(e)
                self.send("🤔 Не нашёл здесь еды. Если это еда — напиши подробнее.",
                          reply_to=e["msg_id"])
            return

        rows, retro, used = [], [], False
        for it in items:
            old = self.retro_target(it, e)
            if old:
                # сообщение уточняет уже записанную позицию — заменяем её, а не считаем второй раз
                it["yesterday"] = False
                tgt = self.replace_entry(old, extra=caption or e["text"])
                retro.append(tgt)
            elif not used:
                tgt, used = e, True
            else:
                tgt = self.s.add_entry(
                    day=e["day"], time=e["time"], kind=e["kind"], text=e["text"],
                    msg_id=e["msg_id"], file_id=e.get("file_id", ""),
                    media_group=e.get("media_group", ""))
            self.apply_item(tgt, it)
            rows.append(tgt)
        if not used:
            e["status"] = ANSWER  # само сообщение было уточнением, отдельной еды в нём нет
            self.s.touch(e)

        if retro and len(retro) == len(rows):
            days = sorted({r["day"] for r in retro})
            head = "✏️ Уточнил запись за " + ", ".join(
                "сегодня" if d == self.today() else fmt_day(d) for d in days) + ":"
            total_day = days[-1]
        else:
            head = "✏️ Исправил:" if edited else ("🧾 Покупки сохранил, в съеденное записал:" if receipt else "🍽")
            total_day = e["day"]
        lines = []
        for r in rows:
            approx = "~" if r["status"] != OK else ""
            tail = "  ❓ уточню ниже" if r["status"] == NEED_ASK else ""
            lines.append(f"• {self.label(r)} — {approx}{fmt_num(r['kcal'], 'ккал')} ккал "
                         f"(Б {fmt_num(r['protein'], 'г')} · Ж {fmt_num(r['fat'], 'г')} · "
                         f"У {fmt_num(r['carbs'], 'г')}){tail}")
        self.send(f"{head}\n" + "\n".join(lines) + "\n" + self.day_total_line(total_day),
                  reply_to=e["msg_id"])
        for r in rows:
            if r["status"] == NEED_ASK:
                self.ask(r)

    def retro_target(self, it, e):
        """Запись из истории, которую уточняет это сообщение (если ИИ так решил)."""
        old = self.s.entry(it.get("updates_id")) if it.get("updates_id") else None
        if not old or old is e or old["status"] not in FOOD_DONE + (NEED_ASK, WAITING):
            return None
        return old

    def replace_entry(self, old, extra=""):
        """Старую строку — в «изменено», вместо неё новая (с тем же днём и временем),
        чтобы отчёт и поправка учли изменение."""
        old["status"] = CHANGED
        for k in NUTRIENTS:
            old[k] = ""
        self.s.touch(old)
        new = self.s.add_entry(day=old["day"], time=old["time"], kind=old["kind"],
                               text=(old["text"] + (f" | уточнено: {extra}" if extra else "")),
                               msg_id=old["msg_id"], file_id=old.get("file_id", ""),
                               answers=old.get("answers", ""))
        d = self.s.day(old["day"])
        if d["phase"] == CLOSED:
            d["phase"] = REPORTED
            self.s.touch_day(d)
            self._reopened = True
        return new

    def apply_item(self, e, it):
        e.update({k: it[k] for k in ("item", "place", "variant", "amount", "grams")})
        nutr = dict(it["nutrients"])
        source = it["source"]
        drow = self.dict_row(it.get("dict_id")) if it["complete"] else None
        if drow:
            ratio = 1.0
            dg, ig = to_num(drow.get("grams"), 0), to_num(it.get("grams"), 0)
            if dg and ig and abs(ig / dg - 1) > 0.05:
                ratio = ig / dg
            nutr = {k: round((to_num(drow.get(k), 0) or 0) * ratio, 1) for k in NUTRIENTS}
            source = "справочник"
        for k in NUTRIENTS:
            e[k] = nutr.get(k, 0)
        e["source"] = source
        if it["complete"]:
            e["status"], e["question"], e["options"] = OK, "", ""
            if source != "справочник" and e["place"] and e["place"].lower() != "дом":
                self.save_to_dict(e)
        else:
            e["status"], e["question"] = NEED_ASK, it["question"]
            e["options"] = " | ".join(it["options"])
        if it.get("yesterday"):
            prev = (d_(e["day"]) - timedelta(days=1)).isoformat()
            e["day"] = prev
            d = self.s.day(prev)
            if d["phase"] == CLOSED:
                d["phase"] = REPORTED
                self.s.touch_day(d)
                self._reopened = True
        self.s.touch(e)

    def dict_row(self, row_id):
        if row_id is None:
            return None
        for r in self.s.dictionary:
            if to_num(r["_row"]) == to_num(row_id):
                return r
        return None

    def save_to_dict(self, e):
        key = (e["place"].lower(), e["item"].lower(), e["variant"].lower(), e["amount"].lower())
        for r in self.s.dictionary:
            if (str(r["place"]).lower(), str(r["item"]).lower(), str(r["variant"]).lower(),
                    str(r["portion"]).lower()) == key:
                return
        row = {"place": e["place"], "item": e["item"], "variant": e["variant"],
               "portion": e["amount"], "grams": e["grams"], "source": e["source"],
               "added": self.now().strftime("%Y-%m-%d")}
        row.update({k: e[k] for k in NUTRIENTS})
        self.s.add_dict(row)

    def apply_answer(self, e, text, media, reply_to=None):
        if str(self.s.state.get("awaiting")) == str(e["id"]):
            self.s.set_state("awaiting", "")
        if e.get("source") == RECEIPT_SRC:
            self.receipt_answer(e, text, "текст", "", reply_to)
            return
        item_cur = {k: e.get(k) for k in ("item", "place", "variant", "amount", "grams")}
        try:
            res = self.ai.refine(e["text"], item_cur, e.get("answers") or "", text, media,
                                 self.dictionary_text(), self.history_text(), self.purchases_text())
        except Exception as ex:
            log.exception("Ошибка уточнения: %s", ex)
            self.send("Не смог обработать ответ, попробую позже. Ответь ещё раз, пожалуйста.",
                      reply_to=reply_to)
            return
        ans = str(res.get("answer_text") or text or "(голос)").strip()
        e["answers"] = (str(e.get("answers") or "") + f"В: {e['question']} — О: {ans}\n")
        self.apply_item(e, res["item"])
        if e["status"] == OK:
            self.send(f"✅ {self.label(e)} — {fmt_num(e['kcal'], 'ккал')} ккал "
                      f"(Б {fmt_num(e['protein'], 'г')} · Ж {fmt_num(e['fat'], 'г')} · "
                      f"У {fmt_num(e['carbs'], 'г')})\n" + self.day_total_line(e["day"]),
                      reply_to=reply_to)
        else:
            self.ask(e)

    def receipt_answer(self, e, text, kind, file_id, msg_id):
        """Ответ на «что из чека съел» — это новая запись еды (с покупками в подсказке)."""
        if str(self.s.state.get("awaiting")) == str(e["id"]):
            self.s.set_state("awaiting", "")
        e["answers"] = str(e.get("answers") or "") + f"О: {text or '(' + kind + ')'}\n"
        e["status"] = RECEIPT
        self.s.touch(e)
        if kind == "текст" and text.strip().lower().rstrip(".!") in ("ничего не ел", "ничего", "нет"):
            self.send("Ок, ничего не записываю 👌", reply_to=msg_id)
            return
        self.s.add_entry(day=e["day"], time=self.stamp(), kind=kind,
                         text=(text + " (из чека)").strip(), status=NEW,
                         msg_id=msg_id or e["msg_id"], file_id=file_id)

    def day_total_line(self, day) -> str:
        kcal = sum(to_num(e.get("kcal"), 0) or 0 for e in self.s.entries_of(day)
                   if e["status"] in COUNTED)
        norm = next((n["value"] for n in self.s.norms if n["key"] == "kcal"), 0)
        label = "сегодня" if day == self.today() else fmt_day(day)
        tail = f" / {fmt_num(norm, 'ккал')}" if norm else ""
        return f"Итого за {label}: {fmt_num(kcal, 'ккал')}{tail} ккал"

    # ---------------- расписание ----------------
    def times(self, day):
        ask, rep, dl = self.t("ask_time"), self.t("report_time"), self.t("deadline")
        ask_dt = at(day, ask)
        rep_dt = at(day, rep)
        if rep_dt < ask_dt:
            rep_dt += timedelta(days=1)
        dl_dt = at(day, dl)
        if dl_dt <= ask_dt:
            dl_dt += timedelta(days=1)
        return ask_dt, rep_dt, dl_dt

    def day_over(self, day, now):
        nxt = (d_(day) + timedelta(days=1)).isoformat()
        return now >= at(nxt, self.t("day_start"))

    def open_of(self, day):
        return [e for e in self.s.entries_of(day) if e["status"] in OPEN]

    def tick(self):
        now = self.now()
        for _ in range(2):  # второй проход — если запись «вчера» переоткрыла закрытый день
            self._reopened = False
            for d in sorted(self.s.days, key=lambda x: x["day"]):
                if d["phase"] == CLOSED or not d["day"]:
                    continue
                try:
                    self.tick_day(d, now)
                except Exception as ex:
                    log.exception("Ошибка в дне %s: %s", d["day"], ex)
            if not self._reopened:
                break

    def tick_day(self, d, now):
        day = d["day"]
        ask_dt, rep_dt, dl_dt = self.times(day)
        if d["phase"] == COLLECT:
            if now < ask_dt:
                return
            d["phase"] = ASKING
            self.s.touch_day(d)
            self.reask(day)
        if d["phase"] == ASKING:
            self.send_questions(day)
            open_ = self.open_of(day)
            if not open_ and now >= rep_dt:
                self.finish_report(d, now)
            elif open_ and now >= dl_dt:
                self.mark_approx(open_)
                self.finish_report(d, now)
            elif open_ and now >= rep_dt:
                self.maybe_remind(d, now, open_)
            return
        if d["phase"] == REPORTED:
            if not self.day_over(day, now):
                return
            self.send_questions(day)
            open_ = self.open_of(day)
            nxt = (d_(day) + timedelta(days=1)).isoformat()
            corr_deadline = at(nxt, self.t("ask_time"))
            if open_ and now < corr_deadline:
                return
            if open_:
                self.mark_approx(open_)
            last_id = self.report_last_id(d)
            late = [e for e in self.s.entries_of(day)
                    if e["status"] in FOOD_DONE and to_num(e["id"], 0) > last_id]
            if late:
                self.send(self.report_text(day, correction=late))
                d["correction_at"] = self.stamp(now)
            d["phase"] = CLOSED
            self.s.touch_day(d)

    def reask(self, day):
        """Вечером: повторить вопросы, на которые не было ответа днём."""
        waiting = [e for e in self.s.entries_of(day) if e["status"] == WAITING]
        if not waiting:
            return
        self.send(f"🕘 Вечерние уточнения ({len(waiting)}) — без них отчёт за "
                  f"{fmt_day(day)} не пришлю:")
        for e in waiting:
            self.ask(e)

    def mark_approx(self, entries):
        for e in entries:
            if e["status"] in (NEED_ASK, WAITING):
                e["status"] = RECEIPT if e.get("source") == RECEIPT_SRC else APPROX
                self.s.touch(e)
        if str(self.s.state.get("awaiting")) in {str(e["id"]) for e in entries}:
            self.s.set_state("awaiting", "")

    def maybe_remind(self, d, now, open_):
        every = float(to_num(self.s.setting("reminder_every"), 30))
        last = d.get("last_reminder")
        if last:
            try:
                last_dt = datetime.strptime(str(last), "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
                if (now - last_dt).total_seconds() < (every - 3) * 60:
                    return
            except ValueError:
                pass
        waiting = [e for e in open_ if e["status"] == WAITING]
        if not waiting:
            return
        lines = "\n".join(f"• {self.label(e)}" for e in waiting)
        self.send(f"⏳ Жду уточнения ({len(waiting)}), без них отчёт за {fmt_day(d['day'])} не пришлю:\n"
                  f"{lines}\nОтветь реплаем на вопросы выше. После {self.s.setting('deadline')} "
                  f"посчитаю примерно.")
        d["last_reminder"] = self.stamp(now)
        self.s.touch_day(d)

    def report_last_id(self, d) -> int:
        m = re.search(r"№(\d+)", str(d.get("report_at") or ""))
        return int(m.group(1)) if m else 0

    def finish_report(self, d, now):
        day = d["day"]
        food = [e for e in self.s.entries_of(day) if e["status"] in FOOD_DONE]
        if food:
            self.send(self.report_text(day))
        else:
            self.send(f"За {fmt_day(day)} нет записей о еде.")
        last_id = max([to_num(e["id"], 0) for e in self.s.entries] + [0])
        d["phase"] = REPORTED
        d["report_at"] = f"{self.stamp(now)}, записи до №{last_id}"
        self.s.touch_day(d)

    # ---------------- отчёт ----------------
    def label(self, e) -> str:
        s = e.get("item") or ""
        if e.get("variant"):
            s += f", {e['variant']}"
        if e.get("place") and str(e["place"]).lower() != "дом":
            s += f" ({e['place']})"
        if e.get("amount"):
            s += f", {e['amount']}"
        return s

    def norms_block(self, totals) -> list[str]:
        lines = []
        tol = self.tol
        for n in self.s.norms or []:
            key = n["key"]
            if key not in NUTRIENTS:
                continue
            name, unit = NUTRIENT_NAMES[key]
            unit = n.get("unit") or unit
            val, norm = totals.get(key, 0), float(n["value"] or 0)
            typ = str(n["type"]).lower()
            if not norm:
                continue
            pct = (val / norm - 1) * 100
            if "больше" in typ:      # «не больше»
                ok, sign = val <= norm * (1 + tol), "≤"
            elif "меньше" in typ:    # «не меньше»
                ok, sign = val >= norm * (1 - tol), "≥"
            else:
                ok, sign = abs(val / norm - 1) <= tol, ""
            icon = "✅" if ok else ("🔺" if pct > 0 else "🔻")
            tail = "" if ok else f" ({pct:+.0f}%)"
            lines.append(f"{icon} {name}: {fmt_num(val, unit)} / {sign}{fmt_num(norm, unit)} {unit}{tail}")
        return lines

    def report_text(self, day, preview=False, correction=None) -> str:
        statuses = COUNTED if preview else FOOD_DONE
        food = [e for e in self.s.entries_of(day) if e["status"] in statuses]
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        if correction is not None:
            head = f"✏️ Поправка к отчёту за {fmt_day(day)}\nДобавлено или исправлено после отчёта:"
            listed = correction
        elif preview:
            head = f"🕐 Промежуточный отчёт за {fmt_day(day)}"
            listed = food
        else:
            head = f"📊 Отчёт за {fmt_day(day)}"
            listed = food
        out = [head]
        if correction is None:
            out += self.norms_block(totals)
            out.append("")
            out.append("🍽 Съедено:")
        approx = False
        for e in sorted(listed, key=lambda x: x["time"]):
            mark = ""
            if e["status"] != OK:
                mark, approx = " ≈", True
            out.append(f"{e['time'][11:16]} {self.label(e) or e['text'][:50]} — "
                       f"{fmt_num(e.get('kcal'), 'ккал')} ккал{mark}")
        if correction is not None:
            out.append("")
            out.append("Новый итог дня:")
            out += self.norms_block(totals)
        if approx:
            out.append("")
            out.append("≈ — без уточнения, значения примерные.")
        return "\n".join(out)
