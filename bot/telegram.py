"""Минимальный клиент Telegram Bot API (через requests)."""
from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger("tg")


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, http_timeout=30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for k, v in list(params.items()):
            if isinstance(v, (dict, list)):
                params[k] = json.dumps(v, ensure_ascii=False)
        r = requests.post(self.base + method, data=params, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def get_updates(self, offset: int, timeout: int = 0):
        return self.call("getUpdates", http_timeout=timeout + 15, offset=offset,
                         timeout=timeout,
                         allowed_updates=["message", "edited_message", "callback_query"])

    def send(self, chat_id, text, reply_to=None, buttons=None):
        """buttons: список строк кнопок [[(текст, callback_data), ...], ...]"""
        markup = None
        if buttons:
            markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                          for row in buttons]}
        rp = None
        if reply_to:
            rp = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        res = None
        for chunk in split_text(text):
            res = self.call("sendMessage", chat_id=chat_id, text=chunk,
                            reply_parameters=rp, reply_markup=markup)
            rp = None
        return res

    def typing(self, chat_id):
        try:
            self.call("sendChatAction", http_timeout=10, chat_id=chat_id, action="typing")
        except Exception as ex:
            log.info("sendChatAction: %s", ex)

    def answer_callback(self, cq_id, text=None):
        try:
            self.call("answerCallbackQuery", callback_query_id=cq_id, text=text)
        except Exception as ex:  # старые нажатия Telegram уже не принимает — не страшно
            log.info("answerCallbackQuery: %s", ex)

    def edit_text(self, chat_id, message_id, text) -> bool:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
            return True
        except Exception as ex:
            log.info("editMessageText: %s", ex)
            return "not modified" in str(ex)

    def download(self, file_id) -> bytes:
        f = self.call("getFile", file_id=file_id)
        url = f"https://api.telegram.org/file/bot{self.token}/{f['file_path']}"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content


def split_text(text, limit=4000):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line)
    if cur:
        parts.append(cur)
    return parts or [""]
