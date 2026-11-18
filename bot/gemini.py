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

NUTR_DOC = ("kcal (ккал), protein, fat, carbs, sugar — только ДОБАВЛЕННЫЙ сахар, fiber (г), "
            "na, k, ca, mg, fe, zn, p (мг: натрий, калий, кальций, магний, железо, цинк, фосфор), "
            "creatine (г) — природный креатин. Он есть только в мясе и рыбе; г на 1 кг СЫРОГО продукта: "
            "сельдь 6.5–10, свинина 5, говядина 4.5, лосось 4.5, тунец 4, треска 3, курица/индейка ~3.4, "
            "фарш ~2.5; молоко ~0.02, яйцо ~0.1 г/шт, растения 0. При готовке теряется до ~30%; "
            "для колбас/ветчины бери по доле мяса. Протеин и спортпит — 0, если на этикетке нет креатина; "
            "caffeine (мг): эспрессо ~60–80 за шот, американо/фильтр ~40 на 100 мл, раф/капучино — по числу шотов, "
            "кола ~10 на 100 мл (Zero так же), чёрный чай ~20, зелёный ~12–15 на 100 мл, энергетик ~30–32 на 100 мл, "
            "тёмный шоколад ~40–80 на 100 г; декаф ~2–5; если на этикетке указан кофеин — бери с этикетки; "
            "satfat (г) — насыщенные жиры (из этикетки, если есть; иначе оценка: молочный жир ~2/3 насыщенные, "
            "свинина/говядина ~35–40% жира, курица ~30%, растительное масло ~15%, кокосовое/пальмовое 50–90%); "
            "omega3 (мг) — только ЭПК+ДГК: лосось/сёмга ~1500–2000 на 100 г, скумбрия/сельдь ~1500–2500, "
            "форель ~1000, тунец ~200–300 (консервы меньше), треска/минтай ~200, креветки ~300, яйцо ~30, "
            "мясо и растения ~0 (ALA из льна/орехов не считай)")

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
  "same_as_id": id записи из истории, если это ПОВТОР того же блюда («как вчера», «то же самое», «опять тот
    же раф») — тогда КБЖУ возьмут из той записи (с пересчётом на граммы); иначе null,
  "nutrients": {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0, "sugar": 0, "fiber": 0,
                "na": 0, "k": 0, "ca": 0, "mg": 0, "fe": 0, "zn": 0, "p": 0,
                "creatine": 0, "caffeine": 0, "satfat": 0, "omega3": 0}
}"""

RULES = f"""Ты помощник дневника питания. Пользователь (Айрат) записывает в Telegram, что съел или выпил:
текстом, голосом или фото (этикетка, ценник, блюдо). Ты превращаешь запись в позиции с КБЖУ и находишь
«слепые зоны» — всё, что заметно влияет на КБЖУ, сахар, клетчатку и минералы, но не указано.

Слепые зоны (если не указано и это меняет результат больше чем на ~10% или ~30 ккал — спроси):
- количество: граммы, мл, штуки, размер порции/стакана (S/M/L, 300/400 мл);
- где куплено или кто производитель (у сетей и брендов разные рецепты);
  Не превращай общее слово в бренд: «в чайхоне», «в столовой», «в кафе», «в шаурмичной» — это тип заведения,
  а не сеть «Чайхона №1» и т.п. Тогда place = как сказал пользователь («чайхона»), а если важно — спроси
  название. Сеть/бренд пиши, только если пользователь назвал его явно или он есть в справочнике с тем же
  названием. Если пользователь прислал название, адрес или ссылку на заведение — place = «Название (адрес)».
  Если у заведения нет опубликованного КБЖУ (у небольших кафе его обычно нет) — source="оценка", не «сайт»;
- вид/вкус/рецепт: раф классический или с сиропом, молоко обычное/овсяное/безлактозное, жирность творога;
- способ приготовления и жир: жарил ли, на каком масле, сколько масла;
- добавки: сахар, сироп, соус, майонез, сыр, хлеб к блюду;
- ДОМАШНЯЯ ЕДА (только place="дом" — сам готовил): соль и мясо. Для кафе, ресторанов, столовых и готовой
  еды из магазина про соль и мясо НЕ спрашивай — бери типичный рецепт такого заведения. Пользователь следит за натрием, поэтому соль — не мелочь.
  Натрий домашнего блюда = природный натрий продуктов (сырое мясо ~60–80 мг/100 г, мука/крупы/овощи ~0–10,
  яйцо ~70/шт, молоко ~45/100 мл) + добавленная соль (1 г соли = 390 мг натрия). НЕ подставляй «типичную»
  соль магазинных или ресторанных блюд. Соль в воду для варки (пельмени, вареники, яйца, картофель
  в мундире) почти вся остаётся в воде — в еду идёт малая часть (~50 мг натрия на порцию); макароны и крупы
  впитывают воду — бери ~25% соли из воды на съеденную долю. Если сколько солил — неизвестно, спроси
  (варианты: «не солил», «чуть-чуть», «как обычно ~1%», «щепотка в воду»), а в оценку пока возьми ~0.5% соли.
  Жир и насыщенные жиры — по мясу: если вид/жирность мяса неизвестны и это заметно меняет КБЖУ (фарш, котлеты,
  пельмени, шашлык), спроси (варианты: «постная говядина», «говядина обычная», «говядина+свинина»).
Не спрашивай о том, что почти не влияет на результат (специи, вода, чай без сахара).

САХАР (поле sugar) — только добавленный («свободный», по ВОЗ): сахар, сиропы, мёд, варенье, сладкие напитки
и соки, сладости, сахар в выпечке, соусах, сладких йогуртах и сырках. НЕ считай естественный сахар цельных
фруктов, ягод, овощей, молока и несладкой молочки (лактоза) — для них sugar = 0. Банан, яблоко, кефир,
творог без добавок, яйца, мясо → sugar 0. Сладкий йогурт → только добавленная часть (общий сахар минус
лактоза ≈ 4–5 г на 100 г). Если не уверен — оцени добавленную часть, а не общий сахар.

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

НЕСКОЛЬКО ФОТО. Если фото несколько (альбом) — рассмотри КАЖДОЕ. Разные продукты на разных фото — разные
позиции; лицевая и обратная сторона одной упаковки — одна позиция (название с лицевой, цифры с обратной).
Подпись и фото дополняют друг друга: каждая еда, названная в подписи, — отдельная позиция, даже если её нет
на фото (например, подпись «3 ломтика, экспонента» к фото хлеба — это хлеб 3 ломтика И напиток Exponenta).
Название позиции — конкретное, как на упаковке («Хлеб Бородинский Коломенское»), а не категория
(«Мучное изделие/выпечка»). В transcript перечисли, что на каждом фото: «фото 1: …; фото 2: …».

ЭТИКЕТКИ. Если на фото (или в описании) есть таблица КБЖУ с упаковки — бери цифры ТОЛЬКО с этикетки,
не оценивай сам: пересчитай на съеденное количество (данные бывают на 100 г, на порцию или на всю упаковку —
смотри внимательно). Соль → натрий: 1 г соли = 393 мг натрия. Сахар на этикетке обычно общий — в sugar запиши только добавленную часть
(за вычетом лактозы молочки и сахаров фруктов/овощей; для несладкого готового блюда — около 0). source="этикетка".
Если упаковка на несколько порций («600 г, 3–4 порции») и не сказано, сколько съел, — complete=false
и спроси, сколько съел (варианты: вся упаковка, половина, порция ~150 г).

Вопрос — один на позицию, коротко, на «ты», можно объединить несколько недостающих деталей.
Не задавай повторно вопрос, на который уже ответили (смотри «Ответы» у записи): если ответ не на тот вопрос —
прими его как есть и посчитай лучшей оценкой.
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
  "request": null,
  "items": [ {item_schema} ]
}}
receipt — только если это чек или список покупок, иначе пустой список.
request — если это не запись еды, а просьба о совете («че купить в ВкусВилл», «чем перекусить»,
«составь меню на завтра», «как добрать белок», «что приготовить на ужин»): верни
{{"mode": "eat" | "recipe" | "plan" | "shop", "wish": "суть просьбы своими словами"}} и пустой items.
eat — что съесть сейчас; recipe — рецепты; plan — меню/рацион на день; shop — что купить."""

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


class _Fail:
    """Подмена ответа, когда запрос не дошёл (таймаут, обрыв соединения)."""

    def __init__(self, text):
        self.status_code, self.text = 504, text


def choose_fallback(names, main):
    """Из списка моделей ключа выбрать запасную: сначала flash-lite, потом другая flash (поновее)."""
    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    bad = ("image", "tts", "audio", "live", "embed", "thinking", "exp", "native", "computer", "robotics")
    ok = [n for n in names if n != main and n.startswith("gemini-") and not any(b in n for b in bad)]
    for pat in (r"^gemini-[\d.]+-flash-lite$", r"^gemini-[\d.]+-flash$", r"^gemini-flash(-lite)?-latest$",
                r"^gemini-[\d.]+-flash-lite-preview", r"^gemini-[\d.]+-flash-preview"):
        cands = sorted([n for n in ok if re.search(pat, n)], key=ver, reverse=True)
        if cands:
            return cands[0]
    return ""


class Gemini:
    def __init__(self, api_key: str, model: str, web_search: bool = True):
        self.key = api_key
        self.model = model
        self.web_search = web_search
        self._auto_fallback = None
        self.no_search = False
        self.fallback = ""  # запасная модель, если основная перегружена (лист «Настройки», fallback_model)

    def _post(self, parts, use_search, json_mode, system=None, model=None):
        body = {
            "systemInstruction": {"parts": [{"text": system or RULES}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if use_search:
            body["tools"] = [{"google_search": {}}]
        r = requests.post(URL.format(model=model or self.model), json=body, timeout=120,
                          headers={"x-goog-api-key": self.key})
        return r

    def pick_fallback(self) -> str:
        """Запасная модель: из настроек, иначе — сама выбирается из моделей, доступных ключу."""
        if self.fallback:
            return self.fallback
        if self._auto_fallback is None:
            self._auto_fallback = ""
            try:
                r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                                 params={"pageSize": 200}, headers={"x-goog-api-key": self.key}, timeout=20)
                names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                         if "generateContent" in m.get("supportedGenerationMethods", [])]
                self._auto_fallback = choose_fallback(names, self.model)
                log.info("Запасная модель: %s", self._auto_fallback or "нет")
            except Exception as ex:
                log.warning("Не получил список моделей: %s", ex)
        return self._auto_fallback

    def _call(self, parts, use_search, system=None):
        """Запрос с повторами. Перегрузка (503/таймаут) — ждём и пробуем запасную модель.
        Кончилась квота (429) — сразу запасная модель; если упёрлись в квоту поиска — отвечаем без поиска."""
        modes = [(True, True), (True, False), (False, True)] if use_search else [(False, True)]
        last_err = None
        for search, json_mode in modes:
            if search and self.no_search:
                continue
            waits = [3, 8, 15, 25]
            quota_models = set()
            retry = 0
            while True:
                model = self.model if retry < 2 and self.model not in quota_models \
                    else (self.pick_fallback() or self.model)
                if model in quota_models:
                    break
                try:
                    r = self._post(parts, search, json_mode, system, model=model)
                except requests.RequestException as ex:   # таймаут/обрыв — как перегрузка
                    r = _Fail(str(ex)[:200])
                if r.status_code == 429 and "quota" in r.text.lower():
                    log.warning("Gemini %s (search=%s): кончилась квота", model, search)
                    quota_models.add(model)
                    continue
                if r.status_code in (429, 500, 502, 503, 504) and retry < len(waits):
                    log.warning("Gemini %s → %s, жду %s с", model, r.status_code, waits[retry])
                    time.sleep(waits[retry])
                    retry += 1
                    continue
                break
            if quota_models and search:
                self.no_search = True   # квота поиска на сегодня, видимо, всё — дальше без него
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

    def suggest(self, mode="eat", **ctx) -> dict:
        """mode="eat" — что съесть сейчас; mode="recipe" — рецепты под пожелание."""
        tasks = {"recipe": RECIPE_TASK, "plan": PLAN_TASK, "shop": SHOP_TASK}
        task = tasks.get(mode, SUGGEST_TASK).format(**ctx) + SUGGEST_FORMAT
        # для покупок в конкретной сети полезен поиск по её ассортименту
        use_search = self.web_search and mode == "shop"
        res = self._call([{"text": task}], use_search=use_search, system=SUGGEST_SYSTEM)
        opts = []
        for o in (res.get("options") or [])[:6 if mode == "plan" else 3]:
            if not isinstance(o, dict) or not o.get("title"):
                continue
            opts.append({
                "title": str(o.get("title")).strip(), "kind": str(o.get("kind") or "").strip(),
                "portion": str(o.get("portion") or "").strip(),
                "ingredients": [str(x).strip() for x in (o.get("ingredients") or []) if str(x).strip()][:12],
                "steps": [str(x).strip() for x in (o.get("steps") or []) if str(x).strip()][:6],
                "kcal": _num(o.get("kcal")), "protein": _num(o.get("protein")),
                "fat": _num(o.get("fat")), "carbs": _num(o.get("carbs")),
                "time": str(o.get("time") or "").strip(), "why": str(o.get("why") or "").strip()})
        return {"intro": str(res.get("intro") or "").strip(), "options": opts,
                "outro": str(res.get("outro") or "").strip()}

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


SUGGEST_SYSTEM = """Ты помощник по питанию. Советуешь еду и простые рецепты, чтобы закрыть дневную норму КБЖУ.
Пиши по-русски, коротко, на «ты». Не выдумывай точные цифры для блюд кафе — давай оценку.
Отвечай строго JSON."""

CONTEXT = """Сейчас {time} ({part_of_day}). День закрывается в {day_end}.
Дневная норма: {norm}
Съедено за сегодня: {eaten}
Осталось до нормы: {left}
Перебор (лучше не добавлять): {over}
Не хватает по минералам и клетчатке: {low}

Недавние покупки из чеков (скорее всего, есть дома):
{purchases}

Что он обычно ест (чаще всего за последний месяц), и где:
{usual}
"""

SUGGEST_TASK = CONTEXT + """
Пожелание: {wish}

Предложи 3 варианта, что съесть сейчас: kind="дома" — из того, что, вероятно, есть дома;
kind="купить" — что купить или заказать, лучше из его привычных мест; kind="приготовить" — быстро приготовить.
Учитывай время суток: поздно вечером — без тяжёлого и сладкого, ближе к ночи — белок и полегче.
Если до нормы осталось мало (меньше ~150 ккал) — скажи об этом в intro и предложи лёгкие варианты.
Если он спрашивает на потом («завтра», «на завтрак») — планируй под тот приём пищи из дневной нормы,
а не под сегодняшний остаток и текущее время."""

RECIPE_TASK = CONTEXT + """
Он хочет: {wish}

Предложи 3 рецепта под это желание (kind="рецепт"). Ориентируйся на его вкусы — то, что он обычно ест,
и на продукты, которые есть дома. Простые, до 20–30 минут, из обычных продуктов.
Порцию подбери так, чтобы она вписывалась в остаток КБЖУ (особенно белок и калории).
Если он планирует на потом («завтра», «на завтрак», «на обед», «утром») — готовь под тот приём пищи:
не смотри на сегодняшний остаток и текущее время, а бери долю дневной нормы (завтрак ≈ 25–30%,
обед ≈ 35%, ужин ≈ 25–30%, перекус ≈ 10%). В intro напиши, под какой приём пищи и сколько ккал/белка заложено.
Для каждого: ингредиенты с граммовкой на одну порцию, 3–5 коротких шагов, КБЖУ на порцию."""

PLAN_TASK = CONTEXT + """
Он просит: {wish}

Составь меню на день из 4–5 приёмов пищи (завтрак, обед, ужин, 1–2 перекуса) — каждый приём отдельным
пунктом options. title — «Завтрак: <что>», time — во сколько (например 08:30), kind — «дома», «купить»
или «приготовить». Если меню на сегодня и часть дня уже прошла — только оставшиеся приёмы, под остаток
КБЖУ. Если на завтра или «на день» — на всю дневную норму. Сумма всех приёмов должна попадать в норму
по калориям и белку (±10%), клетчатка — к норме, добавленный сахар и натрий — не выше нормы.
Ориентируйся на его вкусы и привычные места, используй продукты из покупок. Блюда простые и разные.
Для «приготовить» — ингредиенты с граммовкой и 2–4 коротких шага; для «купить» — что именно и где.
В intro — для какого дня меню и на сколько ккал/белка рассчитано."""

SHOP_TASK = CONTEXT + """
Он спрашивает: {wish}

Посоветуй, что купить (kind="купить"): 3 конкретных продукта или готовых блюда — если назван магазин или
сеть, то из её ассортимента (с названием товара как на полке). Под остаток КБЖУ и время суток,
с учётом его вкусов. portion — сколько съесть; КБЖУ — на эту порцию."""

SUGGEST_FORMAT = """

Верни JSON:
{
  "intro": "одна короткая фраза: что сейчас важнее добрать или чего избегать",
  "options": [
    {"title": "название", "kind": "дома|купить|приготовить|рецепт",
     "portion": "порция, например 250 г или 2 яйца + 1 тост",
     "ingredients": ["яйца — 3 шт", "шпинат — 50 г"], "steps": ["шаг 1", "шаг 2"],
     "time": "время готовки, например 10 мин", "kcal": 0, "protein": 0, "fat": 0, "carbs": 0,
     "why": "одной фразой — почему подходит"}
  ],
  "outro": "одна строка — как это закроет остаток"
}
Для «купить» ингредиенты и шаги можно не указывать."""


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
        "same_as_id": i.get("same_as_id") if isinstance(i.get("same_as_id"), int) else None,
        "nutrients": {k: _num(n.get(k)) for k in NUTRIENTS},
    }
    if not out["complete"] and not out["question"]:
        out["question"] = "Уточни, пожалуйста, количество и где куплено/как приготовлено."
    if out["complete"]:
        out["question"] = ""
        out["options"] = []
    return out
