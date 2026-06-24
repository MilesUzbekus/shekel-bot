"""Логика леджера: применение операции к балансам (общая для импорта и бота).

Источники изменения баланса:
- trade buy:   купил шекели  -> +шекели, -рубли (qty*my_rate); доход = qty*(google-my)
- trade sell:  продал шекели -> -шекели, +рубли (qty*my_rate); доход = qty*(my-google)
- income:      партнёр получил шекели вне сделки -> +шекели, рубли без изменений
- deposit:     пополнение рублей -> +рубли, шекели без изменений
"""


def norm_side(s):
    s = str(s or "").strip().lower()
    if "прод" in s or s == "sell":
        return "sell"
    if "куп" in s or s == "buy":
        return "buy"
    if "зараб" in s or "income" in s or "партн" in s:
        return "income"
    if "попол" in s or "добав" in s or "deposit" in s:
        return "deposit"
    return None


def apply_op(ils, rub, side, qty=0.0, my_rate=None, google_rate=None):
    """Возвращает (new_ils, new_rub, profit)."""
    qty = float(qty or 0)
    if side == "sell":
        my, gg = float(my_rate), float(google_rate)
        return ils - qty, rub + qty * my, qty * (my - gg)
    if side == "buy":
        my, gg = float(my_rate), float(google_rate)
        return ils + qty, rub - qty * my, qty * (gg - my)
    if side == "income":      # qty = шекели
        return ils + qty, rub, 0.0
    if side == "deposit":     # qty = рубли
        return ils, rub + qty, 0.0
    raise ValueError(f"неизвестный тип операции: {side}")
