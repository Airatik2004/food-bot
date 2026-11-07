"""Логика бота: дни, уточнения, отчёты."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .schema import (ANSWER, APPROX, ASKING, CLOSED, COLLECT, FOOD_DONE, NEED_ASK,
                     NEW, NOT_FOOD, NUTRIENT_NAMES, NUTRIENTS, OK, OPEN, REPORTED,
                     WAITING)
from .store import Store, to_num

log = logging.getLogger("logic")
MSK = ZoneInfo("Europe/Moscow")

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAY_FALLBACK_CLOSE = "12:00"  # если утром ничего не писал, день всё равно закрывается

HELP = """Привет! Я записываю, что ты ешь, и считаю КБЖУ.

Как пользоваться:
• Днём просто пиши или говори голосом: «сырный суп из ВкусВилла», «раф из Surf 400 мл». Можно фото этикетки.
• Днём я молчу. В {ask} пришлю вопросы по тому, чего не хватает (граммы, место, вкус...). Отвечай реплаем на вопрос или кнопкой.
• Отчёт приходит в {report}, когда всё уточнено. Напоминаю до {deadline}, потом отчёт с пометкой «примерно».
• Новый день начинается с первого сообщения после {start}. Ночные перекусы идут во вчера, утром пришлю поправку.

Команды:
/report — промежуточный отчёт за сегодня
/status — что ждёт уточнения
/newday — начать новый день сейчас (если не спал)"""


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


def assign_day(ts: datetime, current_day: str, day_start: time) -> str:
    """Логический день: новый день начинается с первого сообщения после day_start."""
    cand = ts.date() if ts.timetz().replace(tzinfo=None) >= day_start else ts.date() - timedelta(days=1)
    if not current_day:
        return cand.isoformat()
    cur = d_(current_day)
    return cand.isoformat() if cand > cur else cur.isoformat()


def fmt_num(v, unit):
    v = to_num(v, 0) or 0
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
        return (dt or self.now()).strftime("%Y-%m-%d %H:%M")

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
        elif "message" in u:
            m = u["message"]
            chat = str(m.get("chat", {}).get("id"))
            if not self.chat_id:
                self.tg.send(chat, f"Твой chat id: {chat}\nДобавь его в секрет TELEGRAM_CHAT_ID на GitHub.")
                return
            if chat != self.chat_id:
                log.info("Сообщение из чужого чата %s — пропускаю", chat)
                return
            self.activity = True
            self.handle_message(m)

    def handle_message(self, m: dict):
        text = (m.get("text") or m.get("caption") or "").strip()
        if text.startswith("/"):
            return self.command(text.split()[0].split("@")[0].lower())
        ts = datetime.fromtimestamp(m["date"], MSK)
        kind, file_id = "текст", ""
        if m.get("voice"):
            kind, file_id = "голос", m["voice"]["file_id"]
        elif m.get("audio"):
            kind, file_id = "аудио", m["audio"]["file_id"]
        elif m.get("photo"):
            kind, file_id = "фото", m["photo"][-1]["file_id"]
        elif not text:
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
        if target:
            media = self.media(kind, file_id)
            self.apply_answer(target, text, media, reply_to=m["message_id"])
            return

        day = assign_day(ts, self.s.state.get("current_day", ""), self.t("day_start"))
        if day != self.s.state.get("current_day"):
            self.s.set_state("current_day", day)
        self.s.day(day)  # создать строку дня, если её нет
        self.s.add_entry(day=day, time=self.stamp(ts), kind=kind, text=text, status=NEW,
                         msg_id=m["message_id"], file_id=file_id)

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
        if cmd in ("/start", "/help"):
            self.send(HELP.format(ask=self.s.setting("ask_time"), report=self.s.setting("report_time"),
                                  deadline=self.s.setting("deadline"), start=self.s.setting("day_start")))
        elif cmd == "/newday":
            day = self.now().date().isoformat()
            self.s.set_state("current_day", day)
            self.s.day(day)
            self.send(f"Начал новый день: {fmt_day(day)}. Всё, что было раньше, — во вчерашнем.")
        elif cmd == "/report":
            day = self.s.state.get("current_day")
            if not day:
                self.send("Сегодня ещё нет записей.")
                return
            self.analyze_pending(day)
            self.send(self.report_text(day, preview=True))
        elif cmd == "/status":
            open_ = [e for e in self.s.entries if e["status"] in OPEN]
            if not open_:
                self.send("Всё уточнено 👌")
            else:
                lines = [f"• {e['day']}: {self.label(e) or e['text'][:40]} — {e['status']}" for e in open_]
                self.send("Ждут разбора или ответа:\n" + "\n".join(lines))
        else:
            self.send("Не знаю такой команды. /help")

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

    def send_questions(self, day):
        for e in self.s.entries_of(day):
            if e["status"] != NEED_ASK:
                continue
            buttons = [[(o, f"o:{e['id']}:{k}")] for k, o in enumerate(e.get("_options") or [])]
            buttons.append([("Другой / свой ответ ✏️", f"x:{e['id']}")])
            r = self.send(f"❓ {self.label(e)}\n{e['question']}", reply_to=e["msg_id"] or None,
                          buttons=buttons)
            self.add_question_id(e, r["message_id"])
            e["status"] = WAITING
            self.asked = True

    def open_questions_text(self):
        return "\n".join(f"{e['id']} | {self.label(e)} | {e['question']}"
                         for e in self.s.entries if e["status"] == WAITING)

    # ---------------- разбор ИИ ----------------
    def dictionary_text(self):
        rows = self.s.dictionary[-400:]
        return "\n".join(f"{r['_row']} | {r['place']} | {r['item']} | {r['variant']} | "
                         f"{r['portion']} | {r['kcal']}" for r in rows)

    def media(self, kind, file_id):
        if not file_id:
            return []
        mime = {"голос": "audio/ogg", "аудио": "audio/mpeg", "фото": "image/jpeg"}.get(kind)
        try:
            return [(self.tg.download(file_id), mime)]
        except Exception as ex:
            log.warning("Не скачал файл: %s", ex)
            return []

    def analyze_pending(self, day):
        for e in list(self.s.entries_of(day)):
            if e["status"] == NEW:
                try:
                    self.analyze_entry(e)
                except Exception as ex:
                    log.exception("Ошибка разбора записи %s: %s", e["id"], ex)

    def analyze_entry(self, e):
        media = self.media(e["kind"], e.get("file_id"))
        res = self.ai.analyze(e["time"][11:16], e["text"], media, self.dictionary_text(),
                              self.open_questions_text())
        tr = str(res.get("transcript") or "").strip()
        if e["kind"] in ("голос", "аудио") and tr:
            e["text"] = "🎤 " + tr
        elif e["kind"] == "фото" and tr:
            e["text"] = (e["text"] + " " if e["text"] else "") + f"[фото: {tr}]"
        target = self.s.entry(res.get("answer_to")) if res.get("answer_to") else None
        if target and target["status"] == WAITING:
            e["status"] = ANSWER
            self.s.touch(e)
            self.apply_answer(target, tr or e["text"], [], reply_to=e["msg_id"])
            return
        items = res.get("items") or []
        if not items:
            e["status"] = NOT_FOOD
            self.s.touch(e)
            return
        for i, it in enumerate(items):
            tgt = e if i == 0 else self.s.add_entry(
                day=e["day"], time=e["time"], kind=e["kind"], text=e["text"],
                msg_id=e["msg_id"], file_id=e.get("file_id", ""))
            self.apply_item(tgt, it)

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
            e["status"], e["question"] = OK, ""
            if source != "справочник" and e["place"] and e["place"].lower() != "дом":
                self.save_to_dict(e)
        else:
            e["status"], e["question"] = NEED_ASK, it["question"]
            e["_options"] = it["options"]
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
        item_cur = {k: e.get(k) for k in ("item", "place", "variant", "amount", "grams")}
        try:
            res = self.ai.refine(e["text"], item_cur, e.get("answers") or "", text, media,
                                 self.dictionary_text())
        except Exception as ex:
            log.exception("Ошибка уточнения: %s", ex)
            self.send("Не смог обработать ответ, попробую позже. Ответь ещё раз, пожалуйста.",
                      reply_to=reply_to)
            return
        ans = str(res.get("answer_text") or text or "(голос)").strip()
        e["answers"] = (str(e.get("answers") or "") + f"В: {e['question']} — О: {ans}\n")
        if str(self.s.state.get("awaiting")) == str(e["id"]):
            self.s.set_state("awaiting", "")
        self.apply_item(e, res["item"])
        if e["status"] == OK:
            self.send(f"✅ {self.label(e)} — {fmt_num(e['kcal'], 'ккал')} ккал, "
                      f"Б {fmt_num(e['protein'], 'г')} / Ж {fmt_num(e['fat'], 'г')} / "
                      f"У {fmt_num(e['carbs'], 'г')}", reply_to=reply_to)
        else:
            # новый вопрос отправим сразу
            self.send_questions(e["day"])

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
        cur = self.s.state.get("current_day") or day
        if cur > day:
            return True
        nxt = (d_(day) + timedelta(days=1)).isoformat()
        return now >= at(nxt, hhmm(DAY_FALLBACK_CLOSE))

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
        if d["phase"] == ASKING:
            self.analyze_pending(day)
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
            self.analyze_pending(day)
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

    def mark_approx(self, entries):
        for e in entries:
            if e["status"] in (NEED_ASK, WAITING):
                e["status"] = APPROX
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
        statuses = FOOD_DONE + ((NEED_ASK, WAITING, NEW) if preview else ())
        food = [e for e in self.s.entries_of(day) if e["status"] in statuses]
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        if correction is not None:
            head = f"✏️ Поправка к отчёту за {fmt_day(day)}\nДобавлено после отчёта:"
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
        for e in listed:
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
