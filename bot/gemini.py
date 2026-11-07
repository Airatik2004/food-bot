"""Работа с Gemini API: разбор записей о еде и уточнений."""
from __future__ import annotations

import base64
import json
import logging
import re
import time

import requests

from .schema import NUTRIENTS

log = logging.getLogger("gemini")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

NUTR_DOC = ("kcal (ккал), protein, fat, carbs, sugar, fiber (г), "
            "na, k, ca, mg, fe, zn, p (мг: натрий, калий, кальций, магний, железо, цинк, фосфор)")

ITEM_SCHEMA = """{
  "item": "название блюда/продукта по-русски, коротко",
  "place": "кафе, магазин или производитель; \\"дом\\" если готовил сам; \\"\\" если неизвестно",
  "variant": "вкус/вид/рецепт, если важно (\\"солёная карамель\\", \\"на овсяном\\"), иначе \\"\\"",
  "amount": "сколько съел, как сказал пользователь или уточнил (\\"400 мл\\", \\"2 шт\\", \\"порция 300 г\\")",
  "grams": число граммов/мл всего съеденного (оценка, если точно неизвестно),
  "dict_id": номер строки справочника, если запись совпадает с ней почти 1 в 1, иначе null,
  "complete": true/false — хватает ли данных, чтобы посчитать КБЖУ без заметной ошибки,
  "question": "вопрос пользователю, если complete=false, иначе null",
  "options": ["2–4 коротких варианта ответа для кнопок, до 30 символов"],
  "source": "справочник" | "сайт" | "оценка",
  "eaten_at": "ЧЧ:ММ, только если пользователь явно назвал время, иначе null",
  "yesterday": true, если пользователь явно сказал, что это было вчера, иначе false,
  "nutrients": {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0, "sugar": 0, "fiber": 0,
                "na": 0, "k": 0, "ca": 0, "mg": 0, "fe": 0, "zn": 0, "p": 0}
}"""

RULES = f"""Ты помощник дневника питания. Пользователь (Айрат) записывает в Telegram, что съел или выпил:
текстом, голосом или фото (этикетка, ценник, блюдо). Ты превращаешь запись в позиции с КБЖУ и находишь
«слепые зоны» — всё, что заметно влияет на КБЖУ, сахар, клетчатку и минералы, но не указано.

Слепые зоны (если не указано и это меняет результат больше чем на ~10% или ~30 ккал — спроси):
- количество: граммы, мл, штуки, размер порции/стакана (S/M/L, 300/400 мл);
- где куплено или кто производитель (у сетей и брендов разные рецепты);
- вид/вкус/рецепт: раф классический или с сиропом, молоко обычное/овсяное/безлактозное, жирность творога;
- способ приготовления и жир: жарил ли, на каком масле, сколько масла;
- добавки: сахар, сироп, соус, майонез, сыр, хлеб к блюду.
Не спрашивай о том, что почти не влияет на результат (соль по вкусу, специи, вода, чай без сахара).

Справочник — блюда, которые пользователь уже уточнял раньше. Правило строгое:
- ставь dict_id, только если место, блюдо, вариант и размер совпадают почти 1 в 1;
- если пользователь назвал не всё (например «раф» без места, вкуса или объёма) или подходит несколько
  строк — complete=false, задай вопрос и дай в options подходящие варианты из справочника
  (коротко: «Классический 400 мл», «Солёная карамель 400 мл»);
- если в справочнике нет, а блюдо из сети/бренда с опубликованным КБЖУ (ВкусВилл, Surf Coffee,
  Чайхона №1, магазинные продукты), найди значения на сайте через поиск, source="сайт"; иначе оцени сам,
  source="оценка".

Вопрос — один на позицию, коротко, на «ты», можно объединить несколько недостающих деталей.
Даже если данных не хватает, заполни nutrients лучшей оценкой для типичной порции.
nutrients — на всё съеденное количество, не на 100 г. Единицы: {NUTR_DOC}.
Если в записи несколько продуктов — несколько позиций. Если сообщение не о еде — пустой список items.
Отвечай строго JSON без пояснений."""

ANALYZE_TASK = """Новая запись пользователя (отправлена {time} по Москве).
{text_block}
Справочник (номер | место | блюдо | вариант | порция | ккал):
{dictionary}

Открытые вопросы бота, на которые пользователь ещё не ответил (id | позиция | вопрос):
{open_questions}
Если сообщение — это не новая еда, а ответ на один из этих вопросов, верни answer_to = id этого вопроса
и пустой items.

Верни JSON:
{{
  "transcript": "текст записи (для голосового — расшифровка, для фото — что на нём)",
  "answer_to": null,
  "items": [ {item_schema} ]
}}"""

REFINE_TASK = """Ранее ты разобрал запись пользователя и задал уточняющий вопрос. Пользователь ответил.
Исходная запись: {text}
Текущий разбор позиции: {item}
История вопросов и ответов:
{history}
Новый ответ: {answer_block}

Справочник (номер | место | блюдо | вариант | порция | ккал):
{dictionary}

Обнови позицию с учётом ответа. Если данных всё ещё не хватает — complete=false и новый вопрос.
Если пользователь говорит «не знаю»/«примерно» — оцени сам и поставь complete=true.
Верни JSON:
{{
  "answer_text": "текст ответа пользователя (для голосового — расшифровка)",
  "item": {item_schema}
}}"""


class Gemini:
    def __init__(self, api_key: str, model: str, web_search: bool = True):
        self.key = api_key
        self.model = model
        self.web_search = web_search

    def _post(self, parts, use_search, json_mode):
        body = {
            "systemInstruction": {"parts": [{"text": RULES}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if use_search:
            body["tools"] = [{"google_search": {}}]
        r = requests.post(URL.format(model=self.model), json=body, timeout=120,
                          headers={"x-goog-api-key": self.key})
        return r

    def ask(self, parts) -> dict:
        """Пробует: поиск + JSON → поиск без JSON-режима → без поиска."""
        attempts = []
        if self.web_search:
            attempts += [(True, True), (True, False)]
        attempts += [(False, True)]
        last_err = None
        for use_search, json_mode in attempts:
            for retry in range(3):
                r = self._post(parts, use_search, json_mode)
                if r.status_code in (429, 500, 503) and retry < 2:
                    time.sleep(5 * (retry + 1))
                    continue
                break
            if r.status_code != 200:
                last_err = f"{r.status_code}: {r.text[:300]}"
                log.warning("Gemini (search=%s json=%s) → %s", use_search, json_mode, last_err)
                continue
            try:
                return parse_json(response_text(r.json()))
            except ValueError as ex:
                last_err = str(ex)
                log.warning("Не разобрал ответ Gemini: %s", ex)
        raise RuntimeError(f"Gemini не ответил: {last_err}")

    def analyze(self, time_str, text, media, dictionary_text, open_q_text) -> dict:
        text_block = f"Текст: {text}" if text else "Текста нет, см. вложение."
        prompt = ANALYZE_TASK.format(time=time_str, text_block=text_block,
                                     dictionary=dictionary_text or "(пусто)",
                                     open_questions=open_q_text or "(нет)",
                                     item_schema=ITEM_SCHEMA)
        res = self.ask([{"text": prompt}] + media_parts(media))
        res.setdefault("items", [])
        res["items"] = [normalize_item(i) for i in res["items"] if isinstance(i, dict)]
        return res

    def refine(self, text, item, history, answer_text, media, dictionary_text) -> dict:
        answer_block = answer_text if answer_text else "(голосовое, см. вложение)"
        prompt = REFINE_TASK.format(text=text, item=json.dumps(item, ensure_ascii=False),
                                    history=history or "(нет)", answer_block=answer_block,
                                    dictionary=dictionary_text or "(пусто)",
                                    item_schema=ITEM_SCHEMA)
        res = self.ask([{"text": prompt}] + media_parts(media))
        res["item"] = normalize_item(res.get("item") or {})
        return res


def media_parts(media):
    parts = []
    for data, mime in media or []:
        parts.append({"inline_data": {"mime_type": mime,
                                      "data": base64.b64encode(data).decode()}})
    return parts


def response_text(data) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise ValueError(f"пустой ответ: {str(data)[:300]}")
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise ValueError(f"нет JSON в ответе: {text[:200]}")


def _num(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return 0


def normalize_item(i: dict) -> dict:
    n = i.get("nutrients") or {}
    out = {
        "item": str(i.get("item") or "").strip(),
        "place": str(i.get("place") or "").strip(),
        "variant": str(i.get("variant") or "").strip(),
        "amount": str(i.get("amount") or "").strip(),
        "grams": _num(i.get("grams")),
        "dict_id": i.get("dict_id") if isinstance(i.get("dict_id"), int) else None,
        "complete": bool(i.get("complete")),
        "question": (str(i.get("question")).strip() if i.get("question") else ""),
        "options": [str(o)[:40] for o in (i.get("options") or []) if str(o).strip()][:4],
        "source": str(i.get("source") or "оценка"),
        "eaten_at": i.get("eaten_at") or None,
        "yesterday": bool(i.get("yesterday")),
        "nutrients": {k: _num(n.get(k)) for k in NUTRIENTS},
    }
    if not out["complete"] and not out["question"]:
        out["question"] = "Уточни, пожалуйста, количество и где куплено/как приготовлено."
    if out["complete"]:
        out["question"] = ""
        out["options"] = []
    return out
