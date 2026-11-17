"""Логика бота: разбор записей, вопросы, дни, отчёты."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .schema import (ANSWER, APPROX, CHANGED, CLOSED, FOOD_DONE, NEED_ASK, NEW, NOT_FOOD,
                     NUTRIENT_NAMES, NUTRIENTS, OK, OPEN, RECEIPT, WAITING)
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
• На каждую запись отвечаю разбором и полной картиной дня: калории, БЖУ, сахар, клетчатка, минералы против нормы. Если чего-то не хватает (граммы, место, вкус), сразу спрошу — отвечай кнопкой или реплаем. Можно «как вчера».
• Чек: пришли фото — сохраню покупки и спрошу, что из этого ты съел. В подписи можно сразу написать: «съел хлеб, 4 куска».
• Ошибся — просто отредактируй сообщение, я пересчитаю.
• Новый день начинается в {start}: всё, что съел ночью, идёт во вчера. В {start} пришлю итог за прошедший день; неотвеченное посчитаю «примерно» (ответить можно и потом).

Команды:
/day — отчёт за сегодня на текущий момент. За другой день: «/day вчера», «/day 15.11», «статистика за понедельник»
/supps — отметить бады (утро / день / вечер / перед тренировкой). Можно текстом: «выпил утренние бады», «бады все», «креатин перед тренировкой»
/eat — что съесть сейчас (с учётом остатка КБЖУ и времени). Можно и текстом: «че съесть», «что бы съесть сладкое»
/recipe — рецепты под желание и твои вкусы. Можно текстом: «хочу что-то с яйцами», «рецепт сырников», «что приготовить из курицы»
/plan — меню на день: «составь меню на завтра», «рацион на сегодня»
/shop — что купить: «че купить в ВкусВилл»
Можно спрашивать и своими словами: «чем перекусить», «как добрать белок» — пойму, что это вопрос, а не еда.
Под советами есть кнопки «✅ Съел …» — запишу выбранный вариант в дневник.
/status — что ждёт уточнения"""

RECIPE_RE = re.compile(r"^\s*(?:/recipe\b|рецепт\w*|что\s+(?:бы\s+)?(?:мне\s+)?приготовить|"
                       r"хочу(?:\s+(?:что|чего)[- ]?(?:то|нибудь))?)\b[\s?!.,:]*(.*)$", re.I | re.S)
PLAN_RE = re.compile(r"^\s*(?:/plan\b|(?:составь|сделай|дай|предложи|распиши)?\s*(?:мне\s+)?"
                     r"(?:меню|рацион|диет\w*|план\s+питания)(?=\s*(?:на\b|$|[?!.,])))[\s?!.,:]*(.*)$", re.I | re.S)
SHOP_RE = re.compile(r"^\s*(?:/shop\b|(?:ч[её]|что|чо|шо)\s+(?:бы\s+)?(?:мне\s+)?купить)\b[\s?!.,:]*(.*)$",
                     re.I | re.S)
BUSY_SRC = "ИИ занят"
DAY_ASK_RE = re.compile(r"^\s*(?:/day\b|/report\b|(?:покажи\s+|дай\s+)?(?:статистик\w*|стат[аы]?|отч[её]т\w*|"
                        r"итог\w*|сводк\w*)\b)\s*(?:за\s+|на\s+)?(.*?)[\s?!.]*$", re.I | re.S)
WEEKDAY_FULL = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]
SUPP_SLOTS = ("утро", "день", "вечер")  # «все за день»
SUPP_EXTRA = ("тренировка",)  # отдельно: не каждый день


def supp_slots(text: str):
    """«выпил утренние бады» → ["утро"]; «бады все» → все три; без времени → [] (покажем кнопки)."""
    t = text.lower()
    if re.search(r"\bвс[её]\b|\bвсе\b", t):
        return list(SUPP_SLOTS)
    out = []
    if re.search(r"трен|\bзал", t):
        out.append("тренировка")
    if re.search(r"утр", t):
        out.append("утро")
    if re.search(r"дн[её]м|\bдень|дневн|обед", t):
        out.append("день")
    if re.search(r"веч", t):
        out.append("вечер")
    return out


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


def parse_day_arg(arg: str, today: str):
    """«вчера», «позавчера», «15.11», «15 ноября», «понедельник», «2026-11-15» → ISO-день; '' → today;
    непонятно → None."""
    t = arg.strip().lower()
    td = d_(today)
    if not t or t in ("сегодня", "сейчас", "today"):
        return today
    if t.startswith("позавчера"):
        return (td - timedelta(days=2)).isoformat()
    if t.startswith("вчера"):
        return (td - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s+дн\w*\s+назад", t)
    if m:
        return (td - timedelta(days=int(m.group(1)))).isoformat()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.match(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", t)
    mon = None
    if m:
        day_n, mon = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else td.year
        year += 2000 if year < 100 else 0
    else:
        m = re.match(r"(\d{1,2})\s+([а-я]+)", t)
        if m:
            day_n = int(m.group(1))
            mon = next((i + 1 for i, name in enumerate(MONTHS) if m.group(2)[:3] == name[:3]), None)
            year = td.year
    if mon:
        try:
            d = date(year, mon, day_n)
        except ValueError:
            return None
        if d > td and not (m and m.lastindex and m.lastindex >= 3 and m.group(3)):
            d = date(year - 1, mon, day_n)
        return d.isoformat()
    for i, name in enumerate(WEEKDAY_FULL):
        if t.startswith(name) or t == WEEKDAYS[i]:
            back = (td.weekday() - i) % 7
            return (td - timedelta(days=back)).isoformat()
    return None


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
        if not m.get("reply_to_message") and (text.lower().startswith("/supp")
                                              or (re.search(r"\bбад|креатин", text, re.I) and len(text) < 60)):
            return self.supps(supp_slots(text), reply_to=m["message_id"])
        if not m.get("reply_to_message"):
            ask = DAY_ASK_RE.match(text)
            if ask and (text.startswith("/") or len(text) < 50):
                day = parse_day_arg(ask.group(1), self.today())
                if day:
                    self.process_new()
                    return self.send(self.day_report(day), reply_to=m["message_id"])
                if text.startswith("/"):
                    return self.send("Не понял, за какой день. Например: /day вчера, /day 15.11, "
                                     "/day понедельник", reply_to=m["message_id"])
            eat, rec = EAT_RE.match(text), RECIPE_RE.match(text)
            plan, shop = PLAN_RE.match(text), SHOP_RE.match(text)
            if plan:
                return self.suggest(plan.group(1).strip() or "меню на день", reply_to=m["message_id"],
                                    mode="plan")
            if shop:
                return self.suggest(shop.group(1).strip(), reply_to=m["message_id"], mode="shop")
            if eat:
                return self.suggest(eat.group(1).strip(), reply_to=m["message_id"])
            if rec:
                return self.suggest(rec.group(1).strip(), reply_to=m["message_id"], mode="recipe")
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

        # Реплай на совет или рецепт бота («сделал второй, но без тоста») — передаём ИИ и сам совет
        rt_text = str((rt or {}).get("text") or "")
        if rt and (rt.get("from") or {}).get("is_bot") and rt_text:
            text = (f"{text}\n[это ответ на сообщение бота — если это рецепт или совет, пользователь съел "
                    f"по нему с указанными изменениями: {rt_text[:1500]}]")

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
                               media_group=first.get("media_group", ""),
                               reply_msg_id=first.get("reply_msg_id", ""))
        self.edited_ids.add(new["id"])

    def handle_callback(self, cq: dict):
        data = cq.get("data", "")
        msg = cq.get("message", {})
        parts = data.split(":")
        if parts[0] == "ate" and len(parts) > 1:
            return self.ate_option(cq, parts[1])
        if parts[0] == "supp" and len(parts) > 1:
            slots = list(SUPP_SLOTS) if parts[1] == "все" else [parts[1]]
            self.tg.answer_callback(cq["id"], "Отмечаю")
            self.tg.edit_text(self.chat_id, msg.get("message_id"),
                              (msg.get("text") or "") + f"\n✔️ {parts[1]}")
            return self.supps(slots, reply_to=None)
        e = self.s.entry(parts[1]) if len(parts) > 1 else None
        if not e or e["status"] not in (WAITING, APPROX):
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
                    {"command": "day", "description": "Сколько съедено за сегодня"},
                    {"command": "supps", "description": "Бады"},
                    {"command": "eat", "description": "Что съесть сейчас"},
                    {"command": "recipe", "description": "Рецепты"},
                    {"command": "plan", "description": "Меню"},
                    {"command": "shop", "description": "Что купить"},
                    {"command": "status", "description": "Что ждёт уточнения"},
                    {"command": "help", "description": "Как пользоваться"}])
            except Exception as ex:
                log.info("setMyCommands: %s", ex)
        if cmd in ("/start", "/help"):
            self.send(HELP.format(start=self.s.setting("day_start")))
        elif cmd in ("/day", "/report"):
            self.process_new()
            self.send(self.day_report(self.today()))
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

    # ---------------- бады ----------------
    def supps(self, slots, reply_to=None):
        if not self.s.supps:
            self.send("Список бадов пуст — заполни лист «Бады» в таблице.", reply_to=reply_to)
            return
        if not slots:
            self.send("💊 Какие бады отметить?", reply_to=reply_to, buttons=[
                [("✅ Утро", "supp:утро"), ("✅ День", "supp:день"), ("✅ Вечер", "supp:вечер")],
                [("🏋️ Перед тренировкой", "supp:тренировка")],
                [("✅ Все за день", "supp:все")]])
            return
        now = self.now()
        day = assign_day(now, self.t("day_start"))
        self.s.day(day)
        done, already = [], []
        for slot in slots:
            item = f"Бады ({slot})"
            if any(e["item"] == item and e["status"] == OK for e in self.s.entries_of(day)):
                already.append(slot)
                continue
            rows = [r for r in self.s.supps if str(r["slot"]).strip().lower().startswith(slot[:3])]
            if not rows:
                continue
            nutr = {k: 0 for k in NUTRIENTS}
            for r in rows:
                cnt = to_num(r.get("count"), 1) or 1
                for k in nutr:
                    if k in r:
                        nutr[k] += (to_num(r.get(k), 0) or 0) * cnt
            names = ", ".join(f"{r['name']} ×{to_num(r.get('count'), 1) or 1}" for r in rows)
            e = self.s.add_entry(day=day, time=self.stamp(now), kind="бад", text=f"бады: {slot}",
                                 item=item, amount=names, status=OK, source="бады", msg_id=reply_to or "")
            for k in NUTRIENTS:
                e[k] = round(nutr[k], 1)
            done.append(f"• {slot}: {names}")
        out = []
        if done:
            out.append("💊 Отметил бады:\n" + "\n".join(done))
        if already:
            out.append("Уже отмечено сегодня: " + ", ".join(already) + ".")
        if not done and not already:
            out.append("В листе «Бады» нет таких приёмов.")
        food = [e for e in self.s.entries_of(day) if e["status"] in COUNTED]
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        lines = [l for l in self.norms_block(totals, live=True)
                 if any(w in l for w in ("Магний", "Цинк", "Креатин", "Омега"))]
        if lines:
            out.append("\n".join(lines))
        self.send("\n\n".join(out), reply_to=reply_to)

    # ---------------- «че съесть» ----------------
    def usual_text(self, days=30):
        """Что он ест чаще всего (для вкуса рекомендаций) + привычные места."""
        since = (d_(self.today()) - timedelta(days=days)).isoformat()
        cnt = {}
        for e in self.s.entries:
            if e["status"] in FOOD_DONE and e["day"] >= since and e.get("item") and e.get("kind") != "бад":
                key = e["item"] + (f" ({e['place']})" if e.get("place") and e["place"].lower() != "дом" else "")
                cnt[key] = cnt.get(key, 0) + 1
        top = sorted(cnt.items(), key=lambda x: -x[1])[:30]
        lines = [f"{k} — {v} раз" for k, v in top]
        places = sorted({str(r["place"]) for r in self.s.dictionary if r.get("place")})
        if places:
            lines.append("Привычные места: " + ", ".join(places[:15]))
        return "\n".join(lines)

    def suggest(self, wish="", reply_to=None, mode="eat", analyze_first=True):
        if analyze_first:
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
        if mode == "recipe" and not wish:
            wish = "что-нибудь вкусное и простое"
        try:
            res = self.ai.suggest(
                mode=mode, time=now.strftime("%H:%M"), part_of_day=part,
                day_end=self.s.setting("day_start"),
                eaten=", ".join(f"{fmt_num(totals[k], NUTRIENT_NAMES[k][1])} {n}" for k, n in
                                (("kcal", "ккал"), ("protein", "г белка"), ("fat", "г жиров"),
                                 ("carbs", "г углеводов"))),
                norm=", ".join(f"{fmt_num(n['value'], NUTRIENT_NAMES[n['key']][1])} "
                               f"{NUTRIENT_NAMES[n['key']][1]} {NUTRIENT_NAMES[n['key']][0].lower()}"
                               for n in self.s.norms if n["key"] in ("kcal", "protein", "fat", "carbs")),
                left=", ".join(left) or "норма закрыта", over=", ".join(over) or "нет",
                low=", ".join(low) or "нет", purchases=self.purchases_text(7) or "(нет)",
                usual=self.usual_text() or "(пока мало данных)", wish=wish or "нет")
        except Exception as ex:
            log.exception("suggest: %s", ex)
            why = ("у Gemini закончился бесплатный лимит запросов — обычно он обновляется к утру"
                   if "quota" in str(ex).lower() else "Gemini сейчас перегружен")
            self.send(f"Не получилось придумать: {why}. Попробуй чуть позже.", reply_to=reply_to)
            return
        opts = res.get("options") or []
        if not opts:
            self.send("Не получилось придумать, попробуй сформулировать иначе.", reply_to=reply_to)
            return
        head = {"recipe": "👨‍🍳 Рецепты" + (f" — {wish}" if wish else ""),
                "plan": "📋 Меню" + (f" — {wish}" if wish else ""),
                "shop": "🛒 Что купить" + (f" — {wish}" if wish else "")}.get(mode, "🍴 Что съесть")
        out = [head]
        if res.get("intro"):
            out.append(res["intro"])
        icons = {"дома": "🏠", "купить": "🛒", "приготовить": "🍳", "рецепт": "🍳"}
        if mode == "plan":
            icons = {k: "" for k in icons}
        for i, o in enumerate(opts, 1):
            out.append("")
            tail = f" · {o['time']}" if o.get("time") else ""
            icon = icons.get(o["kind"].lower(), "•")
            if mode == "plan":
                tail = (f" · {o['time']}" if o.get("time") else "") + f" · {o['kind']}" if o["kind"] else tail
            out.append(f"{i}) {(icon + ' ') if icon else ''}{o['title']}"
                       + (f", {o['portion']}" if o.get("portion") else "") + tail)
            out.append(f"≈ {fmt_num(o['kcal'], 'ккал')} ккал · Б {fmt_num(o['protein'], 'г')} · "
                       f"Ж {fmt_num(o['fat'], 'г')} · У {fmt_num(o['carbs'], 'г')}")
            if o.get("ingredients"):
                out.append("Нужно: " + "; ".join(o["ingredients"]))
            for k, st in enumerate(o.get("steps") or [], 1):
                out.append(f"  {k}. {st}")
            if o.get("why"):
                out.append(f"→ {o['why']}")
        if mode == "plan":
            tot = {k: sum(o[k] for o in opts) for k in ("kcal", "protein", "fat", "carbs")}
            out += ["", f"Итого по меню: ≈ {fmt_num(tot['kcal'], 'ккал')} ккал · Б {fmt_num(tot['protein'], 'г')}"
                        f" · Ж {fmt_num(tot['fat'], 'г')} · У {fmt_num(tot['carbs'], 'г')}"]
        if res.get("outro"):
            out += ["", res["outro"]]
        if mode == "plan":
            names = [re.split(r"[:—-]", o["title"])[0].strip()[:14] or str(i) for i, o in enumerate(opts, 1)]
            buttons = [[(f"✅ {n}", f"ate:{i}")] for i, n in enumerate(names, 1)]
        else:
            buttons = [[(f"✅ Съел {i}", f"ate:{i}") for i in range(1, len(opts) + 1)]]
        self.send("\n".join(out), reply_to=reply_to, buttons=buttons)

    def ate_option(self, cq, n):
        """Кнопка «✅ Съел N» под советом: записать этот вариант как съеденный."""
        msg = cq.get("message", {})
        text = msg.get("text") or ""
        m = re.search(rf"(?ms)^{n}\) (.*?)(?=^\d\) |\Z)", text)
        if not m:
            self.tg.answer_callback(cq["id"], "Не нашёл этот вариант")
            return
        block = m.group(1).strip()
        title = block.split("\n")[0]
        self.tg.answer_callback(cq["id"], "Записываю")
        self.tg.edit_text(self.chat_id, msg.get("message_id"), text + f"\n\n✔️ Съел {n}")
        now = self.now()
        day = assign_day(now, self.t("day_start"))
        self.s.day(day)
        self.s.add_entry(day=day, time=self.stamp(now), kind="текст",
                         text=f"Съел вариант из совета бота: {block}", status=NEW,
                         msg_id=msg.get("message_id"), file_id="")
        log.info("Записан вариант %s: %s", n, title)

    # ---------------- вопросы ----------------
    def entry_by_question(self, msg_id):
        for e in self.s.entries:
            ids = [x for x in str(e.get("question_msg_id") or "").split(",") if x]
            if str(msg_id) in ids and e["status"] in (WAITING, APPROX):
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
        rows = [e for e in self.s.entries if e.get("kind") != "бад"
                and e["status"] in FOOD_DONE + (NEED_ASK, WAITING) and e["day"] >= since]
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
        """Разобрать все новые записи и сразу ответить на каждую. Возвращает те, что не удалось."""
        failed = []
        for e in sorted([x for x in self.s.entries if x["status"] == NEW],
                        key=lambda x: to_num(x["id"], 0)):
            try:
                self.analyze_entry(e)
            except Exception as ex:
                log.exception("Ошибка разбора записи %s: %s", e["id"], ex)
                failed.append(e)
                if e.get("source") != BUSY_SRC and e["status"] == NEW:
                    e["source"] = BUSY_SRC   # сообщаем один раз
                    self.s.touch(e)
                    self.send("⏳ Gemini сейчас перегружен — запишу это, как только он ответит "
                              "(попробую ещё несколько раз).", reply_to=to_num(e.get("msg_id")) or None)
        return failed

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
                req = res.get("request") if isinstance(res.get("request"), dict) else None
                if req and req.get("mode") in ("eat", "recipe", "plan", "shop"):
                    self.suggest(str(req.get("wish") or caption), reply_to=e["msg_id"],
                                 mode=req["mode"], analyze_first=False)
                else:
                    self.send("🤔 Не нашёл здесь еды. Если это еда — напиши подробнее, "
                              "а если вопрос — спроси иначе (/help).", reply_to=e["msg_id"])
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
            head = "✏️ Исправлено:" if edited else ("🧾 Покупки сохранил, в съеденное записал:" if receipt else "🍽")
            total_day = e["day"]
        lines = []
        for r in rows:
            approx = "~" if r["status"] != OK else ""
            tail = "  ❓ уточню ниже" if r["status"] == NEED_ASK else ""
            lines.append(f"• {self.label(r)} — {approx}{fmt_num(r['kcal'], 'ккал')} ккал "
                         f"(Б {fmt_num(r['protein'], 'г')} · Ж {fmt_num(r['fat'], 'г')} · "
                         f"У {fmt_num(r['carbs'], 'г')}){tail}")
        warn = self.limit_warnings(rows, total_day)
        text = (f"{head}\n" + "\n".join(lines) + ("\n\n" + "\n".join(warn) if warn else "")
                + "\n\n" + self.day_block(total_day))
        old_reply = str(e.get("reply_msg_id") or "")
        if edited and old_reply and self.tg.edit_text(self.chat_id, old_reply, text):
            reply_id = old_reply   # исправили свой прежний ответ вместо нового сообщения
        else:
            reply_id = str(self.send(text, reply_to=e["msg_id"])["message_id"])
        for r in rows:
            r["reply_msg_id"] = reply_id
            self.s.touch(r)
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
        self.s.day(old["day"])
        return new

    def apply_item(self, e, it):
        e.update({k: it[k] for k in ("item", "place", "variant", "amount", "grams")})
        nutr = dict(it["nutrients"])
        source = it["source"]
        drow = self.dict_row(it.get("dict_id")) if it["complete"] else None
        same = self.s.entry(it.get("same_as_id")) if it.get("same_as_id") and it["complete"] else None
        if same and same is not e and same["status"] in FOOD_DONE and not drow:
            ratio = 1.0
            sg, ig = to_num(same.get("grams"), 0), to_num(it.get("grams"), 0)
            if sg and ig and abs(ig / sg - 1) > 0.05:
                ratio = ig / sg
            nutr = {k: round((to_num(same.get(k), 0) or 0) * ratio, 1) for k in NUTRIENTS}
            source = "как раньше"
        elif drow:
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
            self.s.day(prev)
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
                      f"У {fmt_num(e['carbs'], 'г')})\n\n"
                      + "".join(w + "\n\n" for w in self.limit_warnings([e], e["day"]))
                      + self.day_block(e["day"]),
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

    def limit_warnings(self, rows, day) -> list[str]:
        """Если одна позиция съедает большую часть лимита «не больше» (натрий, сахар) — предупредить."""
        tips = {"na": "остаток дня лучше без соли: без колбасы, сыра, хлеба, соусов и готовой еды",
                "sugar": "дальше лучше без сладкого и сладких напитков",
                "caffeine": "дальше лучше без кофе, колы и энергетиков",
                "satfat": "дальше лучше нежирное: птица, рыба, крупы, овощи"}
        food = [e for e in self.s.entries_of(day) if e["status"] in COUNTED]
        out = []
        for n in self.s.norms:
            key, norm = n["key"], float(n["value"] or 0)
            if "больше" not in str(n["type"]).lower() or not norm or key not in NUTRIENTS:
                continue
            name = NUTRIENT_NAMES[key][0].lower()
            for r in rows:
                share = (to_num(r.get(key), 0) or 0) / norm
                if share >= 0.4:
                    total = sum(to_num(e.get(key), 0) or 0 for e in food) / norm
                    tail = tips.get(key, "дальше лучше полегче") if total < 1 else \
                        f"за день уже {round(total * 100)}% — {tips.get(key, 'дальше полегче')}"
                    out.append(f"⚠️ {r.get('item') or 'Эта позиция'} — {round(share * 100)}% "
                               f"дневной нормы ({name}); {tail}.")
        cutoff = self.t("caffeine_cutoff")
        start = self.t("day_start")
        for r in rows:
            caf = to_num(r.get("caffeine"), 0) or 0
            try:
                t = datetime.strptime(str(r.get("time"))[:16], "%Y-%m-%d %H:%M").time()
            except ValueError:
                continue
            if caf >= 30 and (t >= cutoff or t < start):
                out.append(f"☕ {r.get('item') or 'Это'} — ≈{round(caf)} мг кофеина после "
                           f"{cutoff.strftime('%H:%M')}: может помешать уснуть.")
        return out

    def day_block(self, day) -> str:
        """Полная картина дня — добавляется к каждому ответу."""
        food = [e for e in self.s.entries_of(day) if e["status"] in COUNTED]
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        label = "Сегодня" if day == self.today() else f"За {fmt_day(day)}"
        live = not self.day_over(day, self.now())
        out = [f"📊 {label}:"] + self.norms_block(totals, live=live)
        if any(e["status"] != OK for e in food):
            out.append("≈ часть позиций примерные")
        return "\n".join(out)

    # ---------------- расписание ----------------
    def day_over(self, day, now):
        nxt = (d_(day) + timedelta(days=1)).isoformat()
        return now >= at(nxt, self.t("day_start"))

    def open_of(self, day):
        return [e for e in self.s.entries_of(day) if e["status"] in OPEN]

    def tick(self):
        """Единственное плановое действие: в 05:00 закрыть прошедший день и прислать итог."""
        now = self.now()
        yesterday = (d_(self.today()) - timedelta(days=1)).isoformat()
        for d in sorted(self.s.days, key=lambda x: x["day"]):
            if d["phase"] == CLOSED or not d["day"] or not self.day_over(d["day"], now):
                continue
            try:
                self.close_day(d, now, send=d["day"] >= yesterday)
            except Exception as ex:
                log.exception("Ошибка в дне %s: %s", d["day"], ex)

    def close_day(self, d, now, send=True):
        day = d["day"]
        self.mark_approx(self.open_of(day))
        if send and any(e["status"] in FOOD_DONE for e in self.s.entries_of(day)):
            self.send(self.summary_text(day))
        d["phase"] = CLOSED
        d["report_at"] = self.stamp(now)
        self.s.touch_day(d)

    def mark_approx(self, entries):
        """Неотвеченные вопросы — в «примерно» (ответить на них можно и позже)."""
        for e in entries:
            if e["status"] in (NEED_ASK, WAITING):
                e["status"] = RECEIPT if e.get("source") == RECEIPT_SRC else APPROX
                self.s.touch(e)
        if str(self.s.state.get("awaiting")) in {str(e["id"]) for e in entries}:
            self.s.set_state("awaiting", "")

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

    def norms_block(self, totals, live=False) -> list[str]:
        """live=True — день ещё идёт: вместо «-90%» показываем, сколько осталось."""
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
            if "больше" in typ:      # «не больше»: превышение видно сразу
                ok, sign = val <= norm, "≤"
            elif "меньше" in typ:    # «не меньше»
                ok, sign = val >= norm * (1 - tol), "≥"
            else:
                ok, sign = abs(val / norm - 1) <= tol, ""
            if live and not ok and pct < 0:
                icon, tail = "⏳", f" (ещё {fmt_num(norm - val, unit)})"
            else:
                icon = "✅" if ok else ("🔻" if pct < 0 else
                                         "⚠️" if sign == "≤" and pct <= tol * 100 else "🔺")
                tail = "" if ok else f" ({pct:+.0f}%)"
            lines.append(f"{icon} {name}: {fmt_num(val, unit)} / {sign}{fmt_num(norm, unit)} {unit}{tail}")
        return lines

    def day_report(self, day) -> str:
        """Отчёт за текущий день «на сейчас» (команда /day)."""
        food = sorted([e for e in self.s.entries_of(day) if e["status"] in COUNTED],
                      key=lambda x: x["time"])
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        live = day == self.today()
        if live:
            out = [f"📊 Сегодня, {fmt_day(day)} — на {self.now().strftime('%H:%M')}"]
        else:
            out = [f"📊 За {fmt_day(day)}"]
        out += self.norms_block(totals, live=live)
        out += ["", "🍽 Съедено:" if food else ("🍽 Пока ничего не записано." if live
                                                else "🍽 За этот день записей нет.")]
        approx = False
        for e in food:
            mark = ""
            if e["status"] != OK:
                mark, approx = " ≈", True
            out.append(f"{e['time'][11:16]} {self.label(e) or e['text'][:50]} — "
                       f"{fmt_num(e.get('kcal'), 'ккал')} ккал{mark}")
        if approx:
            out += ["", "≈ — ждут уточнения, значения примерные."]
        return "\n".join(out)

    def summary_text(self, day) -> str:
        food = sorted([e for e in self.s.entries_of(day) if e["status"] in FOOD_DONE],
                      key=lambda x: x["time"])
        totals = {k: sum(to_num(e.get(k), 0) or 0 for e in food) for k in NUTRIENTS}
        out = [f"🌅 Итог за {fmt_day(day)}"] + self.norms_block(totals) + ["", "🍽 Съедено:"]
        approx = False
        for e in food:
            mark = ""
            if e["status"] != OK:
                mark, approx = " ≈", True
            out.append(f"{e['time'][11:16]} {self.label(e) or e['text'][:50]} — "
                       f"{fmt_num(e.get('kcal'), 'ккал')} ккал{mark}")
        if approx:
            out += ["", "≈ — без уточнения, значения примерные. На вопрос можно ответить и сейчас."]
        return "\n".join(out)
