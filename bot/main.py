"""Точка входа: один запуск бота (его делает GitHub Actions по расписанию)."""
from __future__ import annotations

import json
import logging
import os
import sys
import time

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


def poll(bot: FoodBot, tg: Telegram, timeout=0) -> int:
    offset = int(to_num(bot.s.state.get("last_update_id"), 0)) + 1
    updates = tg.get_updates(offset, timeout=timeout)
    for u in updates:
        try:
            bot.handle_update(u)
        except Exception as ex:
            log.exception("Ошибка обработки апдейта %s: %s", u.get("update_id"), ex)
        bot.s.set_state("last_update_id", u["update_id"])
    return len(updates)


def main():
    tg = Telegram(env("TELEGRAM_TOKEN"))
    store = SheetStore(service_account(), env("SPREADSHEET_ID")).load()
    ai = Gemini(env("GEMINI_API_KEY"), str(store.setting("model")).strip(),
                web_search=str(store.setting("web_search")).strip().lower() in ("да", "yes", "1", "true"))
    bot = FoodBot(store, tg, ai, env("TELEGRAM_CHAT_ID", required=False))

    poll(bot, tg)
    if bot.chat_id:
        bot.tick()
    store.flush()

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
        if poll(bot, tg, timeout=left):
            bot.tick()
            store.flush()
    store.flush()
    log.info("Готово")


if __name__ == "__main__":
    main()
