"""Работа с Google-таблицей как ИСТОЧНИКОМ ПРАВДЫ (формулы в самой таблице).

Бот дописывает СЫРУЮ строку (направление, кол-во, мой курс, гугл-курс, дата, клиент)
+ формулы для C/D/E/H/K/L; Google сам считает балансы. Бот читает балансы/операции
обратно из таблицы. ru-локаль -> разделитель аргументов в формулах ';'.
"""
import re
import uuid
import datetime

import gspread
from gspread.utils import rowcol_to_a1

from src import config, ledger

HEADER_ROW = 4
FIRST_DATA_ROW = 9
COL_SYNC = 13            # M


def a1(row, col):
    return rowcol_to_a1(row, col)


def get_ws():
    gc = gspread.service_account(filename=config.GOOGLE_CREDS_JSON)
    return gc.open_by_key(config.GSHEET_ID).worksheet(config.GSHEET_TAB)


def _newuid():
    return uuid.uuid4().hex[:12]


def _num(s):
    if s is None:
        return None
    t = re.sub(r"[^\d.\-]", "", str(s).replace("\xa0", "").replace(" ", "").replace(",", "."))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def parse_row(row):
    row = (list(row) + [""] * COL_SYNC)[:COL_SYNC]
    A, B, C, D, E, F, G, H, I, J, K, L, M = row
    side = ledger.norm_side(A)
    if side is None:
        return None
    return {"side": side, "A": A, "qty": _num(B), "my": _num(F), "gg": _num(G),
            "client": (J or "").strip(), "date": (I or "").strip(),
            "profit": _num(E), "ils": _num(K), "rub": _num(L), "uid": (M or "").strip()}


# ---------- построение строки с формулами ----------

def make_formula_row(r, direction, qty, my_rate, google_rate, date, client, sync_uid=""):
    """Значения столбцов A..M для строки r: сырьё + формулы (ru-локаль, ';')."""
    A, B, C, D, F, G = f"A{r}", f"B{r}", f"C{r}", f"D{r}", f"F{r}", f"G{r}"
    sell = f'REGEXMATCH(LOWER({A});"прод")'
    buy = f'REGEXMATCH(LOWER({A});"куп")'
    trade = f'REGEXMATCH(LOWER({A});"прод|куп")'
    inj = f'REGEXMATCH(LOWER({A});"влил|добав|полож|занес|внёс|внес")'
    shek = f'REGEXMATCH(LOWER({A});"шекел|шек")'
    c_f = f'=IFERROR(IF({trade};ABS({B})*{F};"");"")'
    d_f = f'=IFERROR(IF({trade};ABS({B})*{G};"");"")'
    e_f = f'=IFERROR(IF({trade};ABS({C}-{D});"");"")'
    h_f = f'=IFERROR(IF({trade};ABS({F}-{G});"");"")'
    kd = f'IF({sell};-ABS({B});IF({buy};ABS({B});IF(AND({inj};{shek});ABS({B});0)))'
    ld = f'IF({sell};{C};IF({buy};-{C};IF(AND({inj};NOT({shek}));N({B});0)))'
    k_f = f'={kd}' if r <= FIRST_DATA_ROW else f'=K{r-1}+{kd}'
    l_f = f'={ld}' if r <= FIRST_DATA_ROW else f'=L{r-1}+{ld}'
    return [direction, qty if qty is not None else "", c_f, d_f, e_f,
            my_rate if my_rate is not None else "", google_rate if google_rate is not None else "",
            h_f, date, client, k_f, l_f, sync_uid]


def _last_data_row(vals):
    last = HEADER_ROW
    for i, row in enumerate(vals, 1):
        if i > HEADER_ROW and row and (row[0] or "").strip():
            last = i
    return last


def append_operation(direction, qty, my_rate, google_rate, date, client):
    """Дописать операцию строкой с формулами; вернуть посчитанные балансы/доход."""
    ws = get_ws()
    vals = ws.get_all_values()
    r = max(_last_data_row(vals) + 1, FIRST_DATA_ROW)
    uid = _newuid()
    rowvals = make_formula_row(r, direction, qty, my_rate, google_rate, date, client, uid)
    ws.update([rowvals], f"A{r}", value_input_option="USER_ENTERED")
    back = ws.get_all_values()
    rr = (list(back[r - 1]) + [""] * COL_SYNC)[:COL_SYNC] if len(back) >= r else [""] * COL_SYNC
    return {"row": r, "uid": uid, "ils": _num(rr[10]), "rub": _num(rr[11]), "profit": _num(rr[4])}


def read_balances():
    vals = get_ws().get_all_values()
    ils = rub = 0.0
    for i, row in enumerate(vals, 1):
        if i <= HEADER_ROW:
            continue
        r = (list(row) + [""] * COL_SYNC)[:COL_SYNC]
        k, l = _num(r[10]), _num(r[11])
        if k is not None and l is not None:
            ils, rub = k, l
    return ils, rub


def read_ops():
    vals = get_ws().get_all_values()
    out = []
    for i, row in enumerate(vals, 1):
        if i <= HEADER_ROW:
            continue
        op = parse_row(row)
        if op:
            op["rownum"] = i
            out.append(op)
    return out


def _raw_last(vals):
    """Сырьё последней строки данных для УДАЛЕНИЯ/ВОССТАНОВЛЕНИЯ — независимо от
    того, распознаёт ли norm_side тип (вливания «влил рубли» norm_side не ловит,
    но удалять/восстанавливать их тоже надо). Возвращает direction/qty/курсы/дата/клиент."""
    r = _last_data_row(vals)
    if r <= HEADER_ROW:
        return None
    row = (list(vals[r - 1]) + [""] * COL_SYNC)[:COL_SYNC]
    return {"row": r, "direction": (row[0] or "").strip(), "qty": _num(row[1]),
            "my_rate": _num(row[5]), "google_rate": _num(row[6]),
            "date": (row[8] or "").strip(), "client": (row[9] or "").strip()}


def peek_last_op():
    """Последняя строка БЕЗ удаления (для подтверждения перед удалением)."""
    return _raw_last(get_ws().get_all_values())


def read_all_rows():
    """ВСЕ строки операций (сделки + вливания) для выгрузки истории — сырые поля,
    независимо от norm_side (вливания тоже попадают)."""
    vals = get_ws().get_all_values()
    out = []
    for i, row in enumerate(vals, 1):
        if i < FIRST_DATA_ROW:
            continue
        r = (list(row) + [""] * COL_SYNC)[:COL_SYNC]
        if not (r[0] or "").strip():
            continue
        out.append({"date": r[8], "op": r[0], "qty": r[1], "my": r[5], "gg": r[6],
                    "profit": r[4], "client": r[9], "ils": r[10], "rub": r[11]})
    return out


def delete_last_op():
    """Удалить последнюю строку; вернуть её сырьё (для возможного восстановления)."""
    ws = get_ws()
    raw = _raw_last(ws.get_all_values())
    if not raw:
        return None
    ws.delete_rows(raw["row"])
    return raw
