"""Хранилище: всё состояние бота живёт в Google Таблице.

Store — данные в памяти (используется и в тестах),
SheetStore — то же самое, но загружает и сохраняет в Google Таблицу.
"""
from __future__ import annotations

import json
from datetime import datetime

from .schema import (DAY_COLS, DICT_COLS, ENTRY_COLS, NUTRIENTS, SHEET_DAYS,
                     SHEET_DICT, SHEET_ENTRIES, SHEET_NORMS, SHEET_SETTINGS,
                     SHEET_STATE, STATE_KEYS, COLLECT, col_letter)


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
    "tolerance": 10, "day_start": "05:00", "ask_time": "21:00",
    "report_time": "21:30", "deadline": "01:00", "reminder_every": 30,
    "live_window": 15, "model": "gemini-3.6-flash", "web_search": "да",
}


class Store:
    def __init__(self):
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.norms: list[dict] = []      # key, name, value, type, unit
        self.entries: list[dict] = []
        self.days: list[dict] = []
        self.dictionary: list[dict] = []
        self.state: dict = {"last_update_id": 0, "current_day": "", "awaiting": ""}
        self._dirty_entries: set[int] = set()
        self._dirty_days: set[str] = set()
        self._new_dict: list[dict] = []
        self._state_dirty = False

    # ---------- записи ----------
    def next_entry_id(self) -> int:
        return max([to_num(e.get("id"), 0) for e in self.entries] + [0]) + 1

    def add_entry(self, **fields) -> dict:
        e = {c: "" for c in ENTRY_COLS}
        e.update(fields)
        e["id"] = self.next_entry_id()
        e["_row"] = max([x["_row"] for x in self.entries] + [1]) + 1
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
        d["_row"] = max([x["_row"] for x in self.days] + [1]) + 1
        self.days.append(d)
        self._dirty_days.add(day)
        return d

    def touch_day(self, d: dict):
        self._dirty_days.add(d["day"])

    # ---------- справочник ----------
    def add_dict(self, item: dict):
        row = {c: "" for c in DICT_COLS}
        row.update({k: v for k, v in item.items() if k in DICT_COLS})
        row["added"] = item.get("added") or datetime.now().strftime("%Y-%m-%d")
        row["_row"] = max([x["_row"] for x in self.dictionary] + [1]) + 1
        self.dictionary.append(row)
        self._new_dict.append(row)
        return row

    # ---------- служебное ----------
    def set_state(self, key, value):
        self.state[key] = value
        self._state_dirty = True

    # ---------- настройки ----------
    def setting(self, key):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def flush(self):
        self._dirty_entries.clear()
        self._dirty_days.clear()
        self._new_dict.clear()
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


def day_formulas(row: int) -> dict:
    """Формулы итогов дня: суммы по листу «Записи» за этот день."""
    f = {}
    src_start = ENTRY_COLS.index("kcal")
    for i, n in enumerate(NUTRIENTS):
        letter = col_letter(src_start + i)
        f[n] = Formula(f"=SUMIFS('{SHEET_ENTRIES}'!{letter}:{letter},"
                       f"'{SHEET_ENTRIES}'!$B:$B,$A{row})")
    st = col_letter(ENTRY_COLS.index("status"))
    f["approx"] = Formula(f"=COUNTIFS('{SHEET_ENTRIES}'!$B:$B,$A{row},"
                          f"'{SHEET_ENTRIES}'!${st}:${st},\"примерно\")")
    return f


class SheetStore(Store):
    def __init__(self, service_account_info: dict, spreadsheet_id: str):
        super().__init__()
        import gspread  # импорт здесь, чтобы тесты работали без gspread
        self.gc = gspread.service_account_from_dict(service_account_info)
        self.sh = self.gc.open_by_key(spreadsheet_id)
        self.row_counts = {}

    def load(self):
        for ws in self.sh.worksheets():
            self.row_counts[ws.title] = ws.row_count
        ranges = [f"'{SHEET_ENTRIES}'!A:AD", f"'{SHEET_DAYS}'!A:S",
                  f"'{SHEET_DICT}'!A:T", f"'{SHEET_NORMS}'!A:E",
                  f"'{SHEET_SETTINGS}'!A:B", f"'{SHEET_STATE}'!A:B"]
        res = self.sh.values_batch_get(
            ranges, params={"valueRenderOption": "UNFORMATTED_VALUE"})
        vr = [x.get("values", []) for x in res["valueRanges"]]
        entries, days, dct, norms, settings, state = vr
        self.entries = _rows_to_dicts(entries, ENTRY_COLS)
        for e in self.entries:
            for k in ("day", "time", "question_msg_id", "file_id", "text"):
                e[k] = str(e[k])
        self.days = _rows_to_dicts(days, DAY_COLS)
        for d in self.days:
            d["day"] = str(d["day"])
        self.dictionary = _rows_to_dicts(dct, DICT_COLS)
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
        return self

    def _ensure_rows(self, title, need):
        have = self.row_counts.get(title, 1000)
        if need > have:
            add = need - have + 500
            self.sh.worksheet(title).add_rows(add)
            self.row_counts[title] = have + add

    def flush(self):
        data = []
        for e in self.entries:
            if e["id"] in self._dirty_entries:
                r = e["_row"]
                data.append({"range": f"'{SHEET_ENTRIES}'!A{r}:{col_letter(len(ENTRY_COLS)-1)}{r}",
                             "values": [[to_cell(e.get(c)) for c in ENTRY_COLS]]})
        if self.entries:
            self._ensure_rows(SHEET_ENTRIES, max(e["_row"] for e in self.entries))
        for d in self.days:
            if d["day"] in self._dirty_days:
                r = d["_row"]
                row = dict(d)
                row.update(day_formulas(r))
                data.append({"range": f"'{SHEET_DAYS}'!A{r}:{col_letter(len(DAY_COLS)-1)}{r}",
                             "values": [[to_cell(row.get(c)) for c in DAY_COLS]]})
        for x in self._new_dict:
            r = x["_row"]
            data.append({"range": f"'{SHEET_DICT}'!A{r}:{col_letter(len(DICT_COLS)-1)}{r}",
                         "values": [[to_cell(x.get(c)) for c in DICT_COLS]]})
        if self._state_dirty:
            vals = [[k, to_cell(self.state.get(k, ""))] for k in STATE_KEYS]
            vals[0][1] = to_num(self.state.get("last_update_id"), 0)
            data.append({"range": f"'{SHEET_STATE}'!A2:B{len(vals)+1}", "values": vals})
        if data:
            self.sh.values_batch_update(
                body={"valueInputOption": "USER_ENTERED", "data": data})
        super().flush()


def load_service_account(raw: str) -> dict:
    return json.loads(raw)
