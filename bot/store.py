"""Хранилище: всё состояние бота живёт в Google Таблице.

Store — данные в памяти (используется и в тестах),
SheetStore — то же самое, но загружает и сохраняет в Google Таблицу.
"""
from __future__ import annotations

import json
from datetime import datetime

from .schema import (EXTRA_NUTRIENTS, NUTRIENT_NAMES, COLLECT, DAY_COLS, DICT_COLS, ENTRY_COLS, NUTRIENTS, PURCHASE_COLS,
                     SHEET_DAYS, SHEET_DICT, SHEET_ENTRIES, SHEET_INBOX, SHEET_NORMS,
                     SHEET_PURCHASES, SHEET_SETTINGS, SHEET_STATE, SHEET_SUPPS, STATE_KEYS,
                     SUPP_COLS, col_letter)


class Formula(str):
    """Строка, которую надо записать как формулу, а не как текст."""


def to_num(v, default=None):
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return default


def to_cell(v):
    if v is None:
        return ""
    if isinstance(v, Formula):
        return str(v)
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s else ""


DEFAULT_SETTINGS = {
    "tolerance": 10, "day_start": "05:00", "caffeine_cutoff": "17:00", "ask_time": "21:00",
    "report_time": "21:30", "deadline": "01:00", "reminder_every": 30,
    "live_window": 15, "model": "gemini-3.6-flash", "web_search": "да",
}


def _next_row(rows):
    return max([x["_row"] for x in rows] + [1]) + 1


class Store:
    def __init__(self):
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.norms: list[dict] = []      # key, name, value, type, unit
        self.entries: list[dict] = []
        self.days: list[dict] = []
        self.dictionary: list[dict] = []
        self.purchases: list[dict] = []
        self.supps: list[dict] = []      # slot, name, count, kcal, fat, mg, zn, note
        self.inbox: list[dict] = []      # update_id, update (dict)
        self.state: dict = {"last_update_id": 0, "current_day": "", "awaiting": ""}
        self.formula_sep = ","
        self._dirty_entries: set[int] = set()
        self._dirty_days: set[str] = set()
        self._new_dict: list[dict] = []
        self._new_purchases: list[dict] = []
        self._state_dirty = False

    # ---------- записи ----------
    def next_entry_id(self) -> int:
        return max([to_num(e.get("id"), 0) for e in self.entries] + [0]) + 1

    def add_entry(self, **fields) -> dict:
        e = {c: "" for c in ENTRY_COLS}
        e.update(fields)
        e["id"] = self.next_entry_id()
        e["_row"] = _next_row(self.entries)
        self.entries.append(e)
        self._dirty_entries.add(e["id"])
        return e

    def touch(self, e: dict):
        self._dirty_entries.add(e["id"])

    def entry(self, eid) -> dict | None:
        eid = to_num(eid)
        for e in self.entries:
            if to_num(e["id"]) == eid:
                return e
        return None

    def entries_of(self, day: str) -> list[dict]:
        return [e for e in self.entries if e["day"] == day]

    # ---------- дни ----------
    def day(self, day: str, create=True) -> dict | None:
        for d in self.days:
            if d["day"] == day:
                return d
        if not create:
            return None
        d = {c: "" for c in DAY_COLS}
        d["day"] = day
        d["phase"] = COLLECT
        d["_row"] = _next_row(self.days)
        self.days.append(d)
        self._dirty_days.add(day)
        return d

    def touch_day(self, d: dict):
        self._dirty_days.add(d["day"])

    # ---------- справочник и покупки ----------
    def add_dict(self, item: dict):
        row = {c: "" for c in DICT_COLS}
        row.update({k: v for k, v in item.items() if k in DICT_COLS})
        row["added"] = item.get("added") or datetime.now().strftime("%Y-%m-%d")
        row["_row"] = _next_row(self.dictionary)
        self.dictionary.append(row)
        self._new_dict.append(row)
        return row

    def add_purchase(self, item: dict):
        row = {c: item.get(c, "") for c in PURCHASE_COLS}
        row["_row"] = _next_row(self.purchases)
        self.purchases.append(row)
        self._new_purchases.append(row)
        return row

    # ---------- служебное ----------
    def set_state(self, key, value):
        self.state[key] = value
        self._state_dirty = True

    # ---------- настройки ----------
    def setting(self, key):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def processed_inbox(self, last_update_id):
        """Отметить, что входящие до last_update_id обработаны (в памяти)."""
        self.inbox = [x for x in self.inbox if x["update_id"] > last_update_id]

    def flush(self):
        self._dirty_entries.clear()
        self._dirty_days.clear()
        self._new_dict.clear()
        self._new_purchases.clear()
        self._state_dirty = False


def _rows_to_dicts(rows, cols):
    out = []
    for i, r in enumerate(rows[1:], start=2):
        if not any(str(x).strip() for x in r):
            continue
        d = {c: (r[j] if j < len(r) else "") for j, c in enumerate(cols)}
        d["_row"] = i
        out.append(d)
    return out


def day_formulas(row: int, sep: str = ",") -> dict:
    """Формулы итогов дня: суммы по листу «Записи» за этот день.
    sep — разделитель аргументов: «;» для русской локали таблицы, «,» для английской."""
    f = {}
    for n in NUTRIENTS:
        letter = col_letter(ENTRY_COLS.index(n))
        f[n] = Formula(f"=SUMIFS('{SHEET_ENTRIES}'!{letter}:{letter}{sep}"
                       f"'{SHEET_ENTRIES}'!$B:$B{sep}$A{row})")
    st = col_letter(ENTRY_COLS.index("status"))
    f["approx"] = Formula(f"=COUNTIFS('{SHEET_ENTRIES}'!$B:$B{sep}$A{row}{sep}"
                          f"'{SHEET_ENTRIES}'!${st}:${st}{sep}\"примерно\")")
    return f


# Локали, где запятая — разделитель дробей, и аргументы формул пишутся через «;»
_DOT_LOCALES = ("en", "ja", "zh", "ko", "th", "he", "hi", "ms", "fil", "ga")


class SheetStore(Store):
    def __init__(self, service_account_info: dict, spreadsheet_id: str):
        super().__init__()
        import gspread  # импорт здесь, чтобы тесты работали без gspread
        self.gc = gspread.service_account_from_dict(service_account_info)
        self.sh = self.gc.open_by_key(spreadsheet_id)
        self.row_counts = {}
        self.col_counts = {}
        self.sheet_ids = {}

    def load(self):
        meta = self.sh.fetch_sheet_metadata(
            {"fields": "properties.locale,sheets.properties"})
        locale = str(meta.get("properties", {}).get("locale", "en_US"))
        self.formula_sep = "," if locale.split("_")[0] in _DOT_LOCALES else ";"
        for s in meta.get("sheets", []):
            p = s["properties"]
            self.row_counts[p["title"]] = p.get("gridProperties", {}).get("rowCount", 1000)
            self.col_counts[p["title"]] = p.get("gridProperties", {}).get("columnCount", 26)
            self.sheet_ids[p["title"]] = p["sheetId"]
        self._ensure_sheets()
        self._ensure_cols(SHEET_ENTRIES, len(ENTRY_COLS))

        L = lambda cols: col_letter(len(cols) - 1)
        ranges = [f"'{SHEET_ENTRIES}'!A:{L(ENTRY_COLS)}", f"'{SHEET_DAYS}'!A:{L(DAY_COLS)}",
                  f"'{SHEET_DICT}'!A:{L(DICT_COLS)}", f"'{SHEET_NORMS}'!A:E",
                  f"'{SHEET_SETTINGS}'!A:B", f"'{SHEET_STATE}'!A:B",
                  f"'{SHEET_PURCHASES}'!A:E", f"'{SHEET_INBOX}'!A:C"]
        if SHEET_SUPPS in self.sheet_ids:
            ranges.append(f"'{SHEET_SUPPS}'!A:{L(SUPP_COLS)}")
        res = self.sh.values_batch_get(
            ranges, params={"valueRenderOption": "UNFORMATTED_VALUE"})
        vr = [x.get("values", []) for x in res["valueRanges"]]
        entries, days, dct, norms, settings, state, purchases, inbox = vr[:8]
        self.supps = _rows_to_dicts(vr[8], SUPP_COLS) if len(vr) > 8 else []
        if entries and len(entries[0]) < len(ENTRY_COLS):  # заголовки новых колонок
            self.sh.values_batch_update(body={"valueInputOption": "RAW", "data": [
                {"range": f"'{SHEET_ENTRIES}'!AE1:{col_letter(len(ENTRY_COLS) - 1)}1",
                 "values": [["Группа фото", "Варианты", "Ответ бота (id)",
                             *[f"{NUTRIENT_NAMES[k][0]}, {NUTRIENT_NAMES[k][1]}"
                               for k in EXTRA_NUTRIENTS]]]}]})
        self.entries = _rows_to_dicts(entries, ENTRY_COLS)
        for e in self.entries:
            for k in ("day", "time", "question_msg_id", "file_id", "text", "media_group",
                      "options", "source", "status", "reply_msg_id"):
                e[k] = str(e[k])
        self.days = _rows_to_dicts(days, DAY_COLS)
        for d in self.days:
            d["day"] = str(d["day"])
        self.dictionary = _rows_to_dicts(dct, DICT_COLS)
        self.purchases = _rows_to_dicts(purchases, PURCHASE_COLS)
        self.norms = []
        for r in norms[1:]:
            if len(r) >= 4 and str(r[0]).strip():
                self.norms.append({"key": str(r[0]).strip(), "name": r[1],
                                   "value": to_num(r[2], 0), "type": str(r[3]).strip(),
                                   "unit": r[4] if len(r) > 4 else ""})
        for r in settings[1:]:
            if len(r) >= 2 and str(r[0]).strip():
                self.settings[str(r[0]).strip()] = r[1]
        for r in state[1:]:
            if len(r) >= 1 and str(r[0]).strip():
                self.state[str(r[0]).strip()] = r[1] if len(r) > 1 else ""
        self.state["last_update_id"] = to_num(self.state.get("last_update_id"), 0)
        self.inbox = []
        self._inbox_rows = []
        for i, r in enumerate(inbox[1:], start=2):
            uid = to_num(r[0] if r else None)
            if uid is None or len(r) < 3:
                continue
            try:
                upd = json.loads(str(r[2]))
            except json.JSONDecodeError:
                continue
            self.inbox.append({"update_id": int(uid), "update": upd})
            self._inbox_rows.append((i, int(uid)))
        return self

    def _ensure_sheets(self):
        """Создать новые листы, если таблица от старой версии бота."""
        need = {SHEET_PURCHASES: ["Дата", "Товар", "Бренд / магазин", "Количество", "Граммы"],
                SHEET_INBOX: ["update_id", "Получено", "JSON"]}
        missing = [t for t in need if t not in self.sheet_ids]
        if missing:
            res = self.sh.batch_update({"requests": [
                {"addSheet": {"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}}}
                for t in missing]})
            for rep in res.get("replies", []):
                p = rep["addSheet"]["properties"]
                self.sheet_ids[p["title"]] = p["sheetId"]
                self.row_counts[p["title"]] = p["gridProperties"]["rowCount"]
            self.sh.values_batch_update(body={"valueInputOption": "RAW", "data": [
                {"range": f"'{t}'!A1", "values": [need[t]]} for t in missing]})

    def _ensure_cols(self, title, need):
        """Добавить колонки, если в листе их меньше, чем нужно новой версии бота."""
        have = self.col_counts.get(title, 26)
        if need > have and title in self.sheet_ids:
            self.sh.batch_update({"requests": [{"appendDimension": {
                "sheetId": self.sheet_ids[title], "dimension": "COLUMNS", "length": need - have}}]})
            self.col_counts[title] = need

    def _ensure_rows(self, title, need):
        have = self.row_counts.get(title, 1000)
        if need > have:
            add = need - have + 500
            self.sh.worksheet(title).add_rows(add)
            self.row_counts[title] = have + add

    def flush(self):
        data = []
        last_col = col_letter(len(ENTRY_COLS) - 1)
        for e in self.entries:
            if e["id"] in self._dirty_entries:
                r = e["_row"]
                data.append({"range": f"'{SHEET_ENTRIES}'!A{r}:{last_col}{r}",
                             "values": [[to_cell(e.get(c)) for c in ENTRY_COLS]]})
        if self.entries:
            self._ensure_rows(SHEET_ENTRIES, max(e["_row"] for e in self.entries))
        for d in self.days:
            if d["day"] in self._dirty_days:
                r = d["_row"]
                row = dict(d)
                row.update(day_formulas(r, self.formula_sep))
                data.append({"range": f"'{SHEET_DAYS}'!A{r}:{col_letter(len(DAY_COLS)-1)}{r}",
                             "values": [[to_cell(row.get(c)) for c in DAY_COLS]]})
        for x in self._new_dict:
            r = x["_row"]
            data.append({"range": f"'{SHEET_DICT}'!A{r}:{col_letter(len(DICT_COLS)-1)}{r}",
                         "values": [[to_cell(x.get(c)) for c in DICT_COLS]]})
        if self._new_purchases:
            self._ensure_rows(SHEET_PURCHASES, max(x["_row"] for x in self.purchases))
        for x in self._new_purchases:
            r = x["_row"]
            data.append({"range": f"'{SHEET_PURCHASES}'!A{r}:E{r}",
                         "values": [[to_cell(x.get(c)) for c in PURCHASE_COLS]]})
        if self._state_dirty:
            vals = [[k, to_cell(self.state.get(k, ""))] for k in STATE_KEYS]
            vals[0][1] = to_num(self.state.get("last_update_id"), 0)
            data.append({"range": f"'{SHEET_STATE}'!A2:B{len(vals)+1}", "values": vals})
        if data:
            self.sh.values_batch_update(
                body={"valueInputOption": "USER_ENTERED", "data": data})
        self._trim_inbox()
        super().flush()

    def _trim_inbox(self):
        """Удалить из «Входящих» обработанные строки в начале листа.
        Новые строки приёмщик дописывает в конец, поэтому удалять начало безопасно."""
        last = int(to_num(self.state.get("last_update_id"), 0))
        rows = getattr(self, "_inbox_rows", [])
        pending = [r for r, uid in rows if uid > last]
        cut = min(pending) if pending else (max((r for r, _ in rows), default=1) + 1)
        n = cut - 2   # строки 2..cut-1: обработанные или пустые
        if n <= 0:
            return
        try:
            self.sh.batch_update({"requests": [{"deleteDimension": {"range": {
                "sheetId": self.sheet_ids[SHEET_INBOX], "dimension": "ROWS",
                "startIndex": 1, "endIndex": 1 + n}}}]})
        except Exception as ex:
            # Google не даёт удалить все строки под шапкой — тогда просто очищаем их
            log.info("Входящие: удалить строки не вышло (%s), очищаю", str(ex)[:120])
            try:
                self.sh.values_clear(f"'{SHEET_INBOX}'!A2:C{cut - 1}")
            except Exception as ex2:
                log.warning("Входящие: не очистил: %s", ex2)
            self._inbox_rows = [(r, u) for r, u in rows if r >= cut]
            return
        self._inbox_rows = [(r - n, u) for r, u in rows if r >= cut]


def load_service_account(raw: str) -> dict:
    return json.loads(raw)
