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
  "updates_id": id записи из истории, если сообщение уточняет уже записанную позицию, иначе null,
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

История — что пользователь ел в последние дни. Если он ссылается на прошлое («как вчера», «тот же»,
«как обычно»), возьми место, вариант и размер из истории и НЕ переспрашивай то, что там уже есть.

Различай два случая:
- новый приём пищи, такой же, как раньше («сегодня опять раф, как вчера») — обычная позиция, updates_id=null;
- уточнение УЖЕ записанной позиции, а не новая еда («вчера был он же» с фото бутылки, «вчерашний сок был
  вот этот», «тот плов был 400 г, а не 300») — верни ту же позицию с уточнёнными данными и
  updates_id = id этой записи из истории. Её не надо считать второй раз.
Одно сообщение может уточнять несколько записей сразу. Например, сегодня «выпил сок 300 мл», потом фото
бутылки с подписью «вчера был он же»: фото уточняет и сегодняшний сок, и вчерашний — верни две позиции,
каждую со своим updates_id (объём и время у каждой — свои, из её записи).
Если непонятно, новая это еда или уточнение, — complete=false и спроси.

Покупки — товары из недавних чеков. Если запись подходит к купленному товару («творог» → в покупках
«Творог с малиной 5% Верейский, 140 г»), используй бренд и вес оттуда.

ЧЕКИ. Чек (фото чека, скриншот заказа, список покупок) — это то, что КУПЛЕНО, а не съедено.
Товары из чека перечисли в receipt. В items клади только то, что пользователь явно назвал съеденным
(например, подпись «хлеб не всю пачку, а 4 штуки» → в items только хлеб, 4 куска, бренд и вид из чека).
Если подпись ничего не говорит о съеденном — items пустой.

Вопрос — один на позицию, коротко, на «ты», можно объединить несколько недостающих деталей.
Даже если данных не хватает, заполни nutrients лучшей оценкой для типичной порции.
nutrients — на всё съеденное количество, не на 100 г. Единицы: {NUTR_DOC}.
Если в записи несколько продуктов — несколько позиций. Если сообщение не о еде — пустой список items.
Отвечай строго JSON без пояснений."""

ANALYZE_TASK = """Новая запись пользователя (отправлена {time} по Москве).
{text_block}
Справочник (номер | место | блюдо | вариант | порция | ккал):
{dictionary}

История за последние дни (id | дата время | блюдо | место | вариант | количество | ккал):
{history}

Покупки из недавних чеков (дата | товар | бренд/магазин | количество):
{purchases}

Открытые вопросы бота, на которые пользователь ещё не ответил (id | позиция | вопрос):
{open_questions}
Если сообщение — это не новая еда, а ответ на один из этих вопросов, верни answer_to = id этого вопроса.
Если при этом оно уточняет и другие записи из истории (например, «вчера был он же»), добавь их в items
с updates_id; новую еду в этом случае в items не добавляй.

Верни JSON:
{{
  "transcript": "для голосового — расшифровка; для фото — коротко, что на нём (без повтора подписи)",
  "answer_to": null,
  "receipt": [ {{"name": "товар", "brand": "бренд или магазин", "amount": "250 г / 1 шт", "grams": 250}} ],
  "items": [ {item_schema} ]
}}
receipt — только если это чек или список покупок, иначе пустой список."""

REFINE_TASK = """Ранее ты разобрал запись пользователя и задал уточняющий вопрос. Пользователь ответил.
Исходная запись: {text}
Текущий разбор позиции: {item}
История вопросов и ответов:
{history}
Новый ответ: {answer_block}

Справочник (номер | место | блюдо | вариант | порция | ккал):
{dictionary}

История за последние дни (дата время | блюдо | место | вариант | количество | ккал):
{recent}

Покупки из недавних чеков (дата | товар | бренд/магазин | количество):
{purchases}

Обнови позицию с учётом ответа. Ответ может ссылаться на историю («как вчера», «тот же»). Если данных всё ещё не хватает — complete=false и новый вопрос.
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

    def _call(self, parts, use_search):
        """Один запрос; при ошибке режима JSON вместе с поиском — повтор без JSON-режима."""
        modes = [(use_search, True)] + ([(True, False)] if use_search else [])
        last_err = None
        for search, json_mode in modes:
            for retry in range(3):
                r = self._post(parts, search, json_mode)
                if r.status_code in (429, 500, 503) and retry < 2:
                    time.sleep(3 * (retry + 1))
                    continue
                break
            if r.status_code != 200:
                last_err = f"{r.status_code}: {r.text[:300]}"
                log.warning("Gemini (search=%s json=%s) → %s", search, json_mode, last_err)
                continue
            try:
                return parse_json(response_text(r.json()))
            except ValueError as ex:
                last_err = str(ex)
                log.warning("Не разобрал ответ Gemini: %s", ex)
        raise RuntimeError(f"Gemini не ответил: {last_err}")

    def ask(self, parts) -> dict:
        """Сначала быстро, без поиска. Если ИИ просит поиск (сетевое блюдо, бренд) — второй заход с поиском."""
        note = ("\n\nПоиск в интернете в этом запросе ВЫКЛЮЧЕН. Если для точного КБЖУ нужно найти "
                "опубликованные данные сети/бренда, а их нет в справочнике и покупках, — всё равно заполни "
                "лучшую оценку и добавь в корень JSON \"need_search\": true. Иначе \"need_search\": false.")
        res = self._call([{"text": parts[0]["text"] + note}] + parts[1:], use_search=False)
        if self.web_search and res.get("need_search"):
            try:
                return self._call(parts, use_search=True)
            except RuntimeError as ex:
                log.warning("Поиск не удался, оставляю оценку: %s", ex)
        return res

    def suggest(self, **ctx) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": SUGGEST_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": SUGGEST_TASK.format(**ctx)}]}],
            "generationConfig": {"temperature": 0.7},
        }
        for retry in range(3):
            r = requests.post(URL.format(model=self.model), json=body, timeout=120,
                              headers={"x-goog-api-key": self.key})
            if r.status_code in (429, 500, 503) and retry < 2:
                time.sleep(5 * (retry + 1))
                continue
            break
        if r.status_code != 200:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
        return response_text(r.json()).strip()

    def analyze(self, time_str, text, media, dictionary_text, open_q_text,
                history_text="", purchases_text="") -> dict:
        text_block = f"Текст: {text}" if text else "Текста нет, см. вложение."
        prompt = ANALYZE_TASK.format(time=time_str, text_block=text_block,
                                     dictionary=dictionary_text or "(пусто)",
                                     history=history_text or "(пусто)",
                                     purchases=purchases_text or "(пусто)",
                                     open_questions=open_q_text or "(нет)",
                                     item_schema=ITEM_SCHEMA)
        res = self.ask([{"text": prompt}] + media_parts(media))
        res["items"] = [normalize_item(i) for i in (res.get("items") or []) if isinstance(i, dict)]
        res["receipt"] = [normalize_purchase(i) for i in (res.get("receipt") or [])
                          if isinstance(i, dict) and i.get("name")]
        return res

    def refine(self, text, item, history, answer_text, media, dictionary_text,
               recent_text="", purchases_text="") -> dict:
        answer_block = answer_text if answer_text else "(голосовое, см. вложение)"
        prompt = REFINE_TASK.format(text=text, item=json.dumps(item, ensure_ascii=False),
                                    history=history or "(нет)", answer_block=answer_block,
                                    dictionary=dictionary_text or "(пусто)",
                                    recent=recent_text or "(пусто)",
                                    purchases=purchases_text or "(пусто)",
                                    item_schema=ITEM_SCHEMA)
        res = self.ask([{"text": prompt}] + media_parts(media))
        res["item"] = normalize_item(res.get("item") or {})
        return res


SUGGEST_SYSTEM = """Ты помощник по питанию. Советуешь, что съесть прямо сейчас, чтобы закрыть дневную норму КБЖУ.
Пиши по-русски, коротко, на «ты», для Telegram: без таблиц и без markdown-заголовков, можно эмодзи.
Не выдумывай точные цифры для конкретных блюд кафе, если не уверен, — пиши «≈»."""

SUGGEST_TASK = """Сейчас {time} ({part_of_day}). День закрывается в {day_end}.
Съедено за сегодня: {eaten}
Осталось до нормы: {left}
Перебор (лучше не добавлять): {over}
Не хватает по минералам и клетчатке: {low}

Недавние покупки из чеков (скорее всего, есть дома):
{purchases}

Где и что он обычно ест (справочник и история):
{usual}

Пожелание: {wish}

Предложи 3 варианта, что съесть сейчас:
1) из того, что, вероятно, есть дома (по покупкам), если подходит;
2) что купить или заказать — лучше из его привычных мест;
3) что быстро приготовить.
Для каждого: что именно и сколько (граммы/штуки), ≈ ккал и Б/Ж/У, и одной фразой — почему подходит.
Учитывай время суток: поздно вечером — без тяжёлого и сладкого, ближе к ночи — белок и полегче.
Если до нормы осталось мало (меньше ~150 ккал) — так и скажи, и предложи лёгкий перекус или ничего.
В конце одной строкой — как это закроет остаток."""


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


def normalize_purchase(i: dict) -> dict:
    return {"name": str(i.get("name") or "").strip(), "brand": str(i.get("brand") or "").strip(),
            "amount": str(i.get("amount") or "").strip(), "grams": _num(i.get("grams"))}


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
        "updates_id": i.get("updates_id") if isinstance(i.get("updates_id"), int) else None,
        "nutrients": {k: _num(n.get(k)) for k in NUTRIENTS},
    }
    if not out["complete"] and not out["question"]:
        out["question"] = "Уточни, пожалуйста, количество и где куплено/как приготовлено."
    if out["complete"]:
        out["question"] = ""
        out["options"] = []
    return out
