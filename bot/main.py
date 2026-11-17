"""Точка входа: один запуск бота (его делает GitHub Actions по расписанию)."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from .gemini import Gemini
from .logic import FoodBot
from .store import SheetStore, to_num
from .telegram import Telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit(f"Не задан секрет {name}")
    return v


def service_account():
    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        a, b = raw.find("{"), raw.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(raw[a:b + 1])
            except json.JSONDecodeError:
                pass
    sys.exit("Секрет GOOGLE_SERVICE_ACCOUNT_JSON — не JSON-ключ. Вставь туда всё содержимое "
             "скачанного .json-файла целиком, от первой { до последней }. "
             f"Сейчас там {len(raw)} символов, начинается не с '{{'." if not raw.lstrip().startswith("{")
             else "Секрет GOOGLE_SERVICE_ACCOUNT_JSON обрезан или испорчен — вставь файл целиком ещё раз.")


class Typing:
    """Пока бот думает, в Telegram висит «печатает…» (Telegram гасит его через 5 с — обновляем)."""

    def __init__(self, tg: Telegram, chat_id, active=True):
        self.tg, self.chat_id, self.active = tg, chat_id, bool(active and chat_id)
        self.stop = threading.Event()

    def _loop(self):
        while not self.stop.is_set():
            self.tg.typing(self.chat_id)
            self.stop.wait(4)

    def __enter__(self):
        if self.active:
            threading.Thread(target=self._loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


def handle_all(bot: FoodBot, updates) -> int:
    last = int(to_num(bot.s.state.get("last_update_id"), 0))
    n = 0
    for u in sorted(updates, key=lambda x: x["update_id"]):
        if u["update_id"] <= last:
            continue  # уже обработано (Telegram мог прислать повтор)
        try:
            bot.handle_update(u)
        except Exception as ex:
            log.exception("Ошибка обработки апдейта %s: %s", u.get("update_id"), ex)
        last = u["update_id"]
        bot.s.set_state("last_update_id", last)
        n += 1
    return n


def poll(bot: FoodBot, tg: Telegram, timeout=0) -> int:
    offset = int(to_num(bot.s.state.get("last_update_id"), 0)) + 1
    return handle_all(bot, tg.get_updates(offset, timeout=timeout))


def receiver_alive(stamp, minutes=10) -> bool:
    """Приёмщик (Apps Script) отметился недавно — значит, сообщения забирает он."""
    try:
        t = datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - t < timedelta(minutes=minutes)


def step(bot: FoodBot, store, retries=2, pause=45):
    if bot.chat_id:
        failed = bot.process_new()   # сразу разобрать новые записи и ответить
        for _ in range(retries):     # Gemini перегружен — подождать и попробовать ещё
            if not failed:
                break
            store.flush()
            time.sleep(pause)
            failed = bot.process_new()
        bot.tick()          # итоги дня
    store.flush()


def main():
    tg = Telegram(env("TELEGRAM_TOKEN"))
    store = SheetStore(service_account(), env("SPREADSHEET_ID")).load()
    ai = Gemini(env("GEMINI_API_KEY"), str(store.setting("model")).strip(),
                web_search=str(store.setting("web_search")).strip().lower() in ("да", "yes", "1", "true"))
    ai.fallback = str(store.setting("fallback_model") or "").strip()
    bot = FoodBot(store, tg, ai, env("TELEGRAM_CHAT_ID", required=False))

    if receiver_alive(store.state.get("receiver")):
        # Режим «приёмщика»: Apps Script раз в минуту забирает сообщения у Telegram,
        # складывает их в лист «Входящие» и запускает этот workflow.
        last = int(to_num(store.state.get("last_update_id"), 0))
        fresh = [x["update"] for x in store.inbox if x["update_id"] > last]
        with Typing(tg, bot.chat_id, active=fresh):
            handle_all(bot, fresh)
            step(bot, store)
        log.info("Готово (приёмщик)")
        return

    # Режим без приёмщика: забираем сообщения у Telegram сами.
    offset = int(to_num(store.state.get("last_update_id"), 0)) + 1
    updates = tg.get_updates(offset)
    with Typing(tg, bot.chat_id, active=updates):
        handle_all(bot, updates)
        step(bot, store)

    # «Живое окно»: если бот только что задал вопросы или ты что-то ответил,
    # он ещё несколько минут слушает ответы в реальном времени.
    window = float(to_num(store.setting("live_window"), 15)) * 60
    deadline = time.time() + window
    while bot.chat_id and (bot.asked or bot.activity) and time.time() < deadline:
        if not any(e["status"] == "ждёт ответа" for e in store.entries):
            break
        left = int(min(50, deadline - time.time()))
        if left <= 0:
            break
        updates = tg.get_updates(int(to_num(store.state.get("last_update_id"), 0)) + 1, timeout=left)
        if updates:
            with Typing(tg, bot.chat_id):
                handle_all(bot, updates)
                step(bot, store)
    store.flush()
    log.info("Готово")


if __name__ == "__main__":
    main()
