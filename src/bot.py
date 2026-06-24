"""Telegram-бот учёта шекель/рубль: ввод сделок, балансы, прогноз, Q&A.
Закрыт на TG_ALLOWED_IDS. Токен и ключ OpenAI берутся из vault через config.
Запуск:  python -m src.bot   (из корня проекта)
"""
import csv
import datetime
import io
import logging
import os
import sys
import glob
import time
import socket
import subprocess
import urllib.request

import httpx
from telegram import (Update, ReplyKeyboardMarkup,
                      InlineKeyboardMarkup, InlineKeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)
from telegram.request import HTTPXRequest

from src import config, storage, data, analytics, ledger, llm, sheets, ai_forecast

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shekel-bot")

# КРИТИЧНО: httpx логирует полный URL Telegram API с токеном внутри.
# Глушим, чтобы токен никогда не попадал в логи/файлы.
for _noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "apscheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

ALLOWED = set(config.TG_ALLOWED_IDS)

# Время старта и режим связи — для /status (заполняются в main()).
_STARTED = None
_CONN = "?"

# Быстрые кнопки (устойчивая клавиатура под полем ввода)
KB = ReplyKeyboardMarkup(
    [["Курс", "Баланс"], ["Прогноз", "Статистика"],
     ["История", "Статус"], ["🗑 Удалить строку"]],
    resize_keyboard=True)
# старый ярлык «Отменить» оставляем распознаваемым — у пользователей он мог
# закэшироваться в клиенте; теперь он ведёт в безопасное меню с подтверждением.
_BTN = {"Курс": "now", "Баланс": "balance", "Прогноз": "forecast",
        "Статистика": "stats", "История": "history", "Статус": "status",
        "🗑 Удалить строку": "undo", "Удалить строку": "undo", "Отменить": "undo"}


def _fmt_row(d):
    """Человекочитаемое описание строки операции для подтверждений/сообщений."""
    parts = [d.get("direction") or "?"]
    if d.get("qty") is not None:
        parts.append(f"{d['qty']:,.0f}")
    if d.get("my_rate") is not None:
        parts.append(f"по {d['my_rate']}")
    if d.get("google_rate") is not None:
        parts.append(f"гугл {d['google_rate']}")
    if d.get("date"):
        parts.append(str(d["date"]))
    if d.get("client"):
        parts.append(str(d["client"]))
    return " ".join(str(p) for p in parts)


def role_of(update: Update):
    """owner (в ALLOWED) / editor / viewer / None (нет доступа)."""
    u = update.effective_user
    if not u:
        return None
    if u.id in ALLOWED:
        return "owner"
    try:
        return storage.get_role(u.id)
    except Exception:
        return None


def ok(update: Update) -> bool:        # может СМОТРЕТЬ
    return role_of(update) in ("owner", "editor", "viewer")


def can_edit(update: Update) -> bool:  # может МЕНЯТЬ
    return role_of(update) in ("owner", "editor")


def is_owner(update: Update) -> bool:
    return role_of(update) == "owner"


async def deny(update: Update):
    await update.message.reply_text("Бот приватный. Введи пароль доступа.")


async def deny_edit(update: Update):
    await update.message.reply_text("У тебя доступ только на просмотр — менять/добавлять нельзя.")


def access_password() -> str:
    return config.vault_secret("shekel_bot_access_password").strip()


def viewer_password() -> str:
    return config.vault_secret("shekel_bot_viewer_password").strip()


def today_iso():
    return datetime.date.today().isoformat()


def _date_disp(iso):
    try:
        return datetime.date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%Y")
    except Exception:
        return iso


def _month_key(d):
    d = (d or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.datetime.strptime(d, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return "?"


def _sheet_totals():
    trades = [o for o in sheets.read_ops() if o["side"] in ("buy", "sell")]
    return sum(o["profit"] or 0 for o in trades), len(trades)


# ---------- расчёт прогноза (общий для /forecast и рассылки) ----------

def build_forecast_text():
    """ИИ-прогноз (рассуждение по новостям+динамике). При сбое ИИ/сети —
    запасной статистический отчёт, чтобы команда не падала."""
    storage.init()
    snap = data.current_snapshot()
    storage.upsert_rate(snap["date"], snap["usd_rub"], snap["usd_ils"], snap["ils_rub"], snap["source"])
    rows = storage.get_series(540)
    try:
        ai_forecast.evaluate_due(rows)      # сверить созревшие прогнозы с фактом (обучение)
    except Exception as e:
        log.warning("evaluate_due failed: %s", e)
    try:
        return ai_forecast.build_forecast(snap, rows)
    except Exception as e:
        log.warning("AI forecast failed -> stats fallback: %s", e)
        cross = [r["ils_rub"] for r in rows]
        sigma = analytics.ewma_vol(analytics.log_returns(cross))
        bias = analytics.compute_bias(rows)
        cones = {label: analytics.cone(snap["ils_rub"], sigma, days)
                 for label, days in config.HORIZONS.items()}
        ils_bal, rub_bal = sheets.read_balances()
        rec = analytics.recommend({"rub_balance": rub_bal, "ils_balance": ils_bal}, snap["ils_rub"], bias)
        return (analytics.format_report(snap, cones, bias, rec, config.ASSUMPTIONS)
                + "\n\n(ИИ-анализ сейчас недоступен — показан статистический прогноз)")


# ---------- команды ----------

async def cmd_start(update, context):
    if not ok(update):
        await deny(update)
        return
    await update.message.reply_text(
        "Привет! Я веду твой учёт шекель/рубль.\n\n"
        "Просто пиши мне сделки обычным текстом, например:\n"
        "• «продал 5000 по 26.2, гугл 24.9, Игорь»\n"
        "• «купил 3000 по 24, гугл 24.7, Пётр»\n"
        "• «партнёр получил 1500 шекелей» / «влил 2000 шекелей»\n"
        "• «влил 50000 рублей»\n\n"
        "Команды:\n"
        "/now — курс и балансы сейчас\n"
        "/balance — остатки и доход\n"
        "/forecast — прогноз на 1н/2н/мес + рекомендация\n"
        "/stats — доход по месяцам\n"
        "/history — выгрузить все сделки файлом (CSV для Excel)\n"
        "/status — здоровье бота (аптайм, связь, бэкап, сделки)\n"
        "/undo — удалить последнюю строку (спросит подтверждение)\n"
        "/restore — вернуть последнюю удалённую строку\n"
        "А ещё можешь просто спросить: «сколько заработал в мае?»",
        reply_markup=KB)


async def cmd_now(update, context):
    if not ok(update):
        await deny(update)
        return
    await update.message.reply_text("Считаю...")
    try:
        snap = data.current_snapshot()
        rows = storage.get_series(540)
        bias = analytics.compute_bias(rows) if rows else {"score": 0}
        ils_bal, rub_bal = sheets.read_balances()
        arrow = "↑" if bias["score"] > 0.1 else ("↓" if bias["score"] < -0.1 else "→")
        breakdown = ""
        if snap.get("usd_rub") and snap.get("usd_ils"):
            breakdown = f"(USD/RUB {snap['usd_rub']:.2f} / USD/ILS {snap['usd_ils']:.4f})\n"
        await update.message.reply_text(
            f"Курс ILS/RUB: {snap['ils_rub']:.3f} руб/шек  [{snap.get('source','?')}]\n"
            f"{breakdown}"
            f"Наклон: {arrow} ({bias['score']:+.2f})\n\n"
            f"Шекели у партнёра: {ils_bal:,.0f} ₪\n"
            f"Рубли у тебя:  {rub_bal:,.0f} ₽",
            reply_markup=KB)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def cmd_balance(update, context):
    if not ok(update):
        await deny(update)
        return
    ils_bal, rub_bal = sheets.read_balances()
    profit, n = _sheet_totals()
    await update.message.reply_text(
        f"Шекели у партнёра: {ils_bal:,.0f} ₪\n"
        f"Рубли у тебя:  {rub_bal:,.0f} ₽\n"
        f"Доход всего:   {profit:,.0f} ₽ ({n} сделок)",
        reply_markup=KB)


async def cmd_stats(update, context):
    if not ok(update):
        await deny(update)
        return
    from collections import defaultdict
    agg = defaultdict(lambda: [0.0, 0])
    for o in sheets.read_ops():
        if o["side"] in ("buy", "sell") and o["date"]:
            agg[_month_key(o["date"])][0] += o["profit"] or 0
            agg[_month_key(o["date"])][1] += 1
    if not agg:
        await update.message.reply_text("Сделок пока нет.")
        return
    lines = ["Доход по месяцам:"]
    tot, cnt = 0.0, 0
    for m in sorted(agg):
        p, n = agg[m]
        tot += p
        cnt += n
        lines.append(f"  {m}: {n:>2} сделок,  {p:,.0f} ₽")
    lines.append(f"\nИтого: {tot:,.0f} ₽ ({cnt} сделок)")
    await update.message.reply_text("\n".join(lines), reply_markup=KB)


async def cmd_history(update, context):
    """Выгрузка ВСЕЙ истории операций файлом CSV (открывается в Excel)."""
    if not ok(update):
        await deny(update)
        return
    await update.message.reply_text("Собираю историю...")
    try:
        rows = sheets.read_all_rows()
    except Exception as e:
        await update.message.reply_text(f"Не смог прочитать таблицу: {e}")
        return
    if not rows:
        await update.message.reply_text("Операций пока нет.", reply_markup=KB)
        return
    # CSV с ';' (ru-Excel) и BOM, чтобы кириллица и числа открывались корректно
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Операция", "Кол-во ₪", "Мой курс", "Курс гугл",
                "Доход ₽", "Клиент", "Шекели у партнёра", "Рубли у тебя"])
    for r in rows:
        w.writerow([r["date"], r["op"], r["qty"], r["my"], r["gg"],
                    r["profit"], r["client"], r["ils"], r["rub"]])
    bio = io.BytesIO(("﻿" + buf.getvalue()).encode("utf-8"))
    bio.name = f"sdelki-{today_iso()}.csv"

    trades = [r for r in rows if ledger.norm_side(r["op"]) in ("buy", "sell")]
    profit = sum(sheets._num(r["profit"]) or 0 for r in trades)
    try:
        ils_bal, rub_bal = sheets.read_balances()
        bal = f"\nСейчас: {ils_bal:,.0f} ₪ / {rub_bal:,.0f} ₽"
    except Exception:
        bal = ""
    cap = (f"История: {len(rows)} операций ({len(trades)} сделок), "
           f"доход {profit:,.0f} ₽{bal}")
    await update.message.reply_document(document=bio, filename=bio.name, caption=cap)


async def cmd_status(update, context):
    """Здоровье бота: аптайм, связь, бэкап, курс, сделки, последний прогноз."""
    if not ok(update):
        await deny(update)
        return
    L = ["Состояние бота:"]
    if _STARTED:
        up = datetime.datetime.now() - _STARTED
        h, m = int(up.total_seconds() // 3600), int((up.total_seconds() % 3600) // 60)
        L.append(f"• Работает без перерыва: {h} ч {m} мин")
    L.append(f"• Связь с Telegram: {_CONN}")
    if _HB["fails"]:
        L.append(f"• Сбоев связи подряд сейчас: {_HB['fails']}")
    wk = glob.glob(os.path.join(str(config.BASE_DIR), "backups", "weekly", "shekel-backup-*"))
    if wk:
        ts = datetime.datetime.fromtimestamp(max(os.path.getmtime(f) for f in wk))
        L.append(f"• Последний бэкап: {ts:%d.%m %H:%M}")
    try:
        snap = data.current_snapshot()
        L.append(f"• Курс сейчас: {snap['ils_rub']:.3f} руб/шек")
    except Exception:
        L.append("• Курс: не удалось получить")
    try:
        rows = sheets.read_all_rows()
        trades = [r for r in rows if ledger.norm_side(r["op"]) in ("buy", "sell")]
        profit = sum(sheets._num(r["profit"]) or 0 for r in trades)
        L.append(f"• Операций в таблице: {len(rows)} (сделок {len(trades)}), доход {profit:,.0f} ₽")
    except Exception:
        L.append("• Таблица: не удалось прочитать")
    try:
        storage.init()
        rec = storage.recent_ai_forecasts(1)
        if rec:
            f0 = rec[0]
            L.append(f"• Последний ИИ-прогноз: {f0['made_date']} (нед {f0['dir_1w']} / мес {f0['dir_1m']})")
    except Exception:
        pass
    await update.message.reply_text("\n".join(L), reply_markup=KB)


async def cmd_forecast(update, context):
    if not ok(update):
        await deny(update)
        return
    await update.message.reply_text("Считаю прогноз...")
    try:
        await update.message.reply_text(build_forecast_text(), reply_markup=KB)
    except Exception as e:
        await update.message.reply_text(f"Ошибка прогноза: {e}")


async def cmd_undo(update, context):
    """Кнопка «Удалить строку» / /undo — НЕ удаляет сразу, а спрашивает подтверждение."""
    if not ok(update):
        await deny(update)
        return
    if not can_edit(update):
        await deny_edit(update)
        return
    try:
        last = sheets.peek_last_op()
    except Exception as e:
        await update.message.reply_text(f"Не смог прочитать таблицу: {e}")
        return
    if not last:
        await update.message.reply_text("В таблице нет строк для удаления.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Удалить", callback_data="undo:confirm"),
        InlineKeyboardButton("Отмена", callback_data="undo:cancel")]])
    await update.message.reply_text(
        "Удалить последнюю строку?\n\n"
        f"• {_fmt_row(last)}\n\n"
        "После удаления можно будет восстановить кнопкой «Восстановить» или /restore.",
        reply_markup=kb)


async def on_undo_callback(update, context):
    """Обработка инлайн-кнопок удаления/восстановления."""
    q = update.callback_query
    await q.answer()
    if not can_edit(update):
        await q.edit_message_text("У тебя доступ только на просмотр — менять нельзя.")
        return
    action = (q.data or "undo:").split(":", 1)[1]

    if action == "cancel":
        await q.edit_message_text("Ок, ничего не удалил.")
        return

    if action == "confirm":
        try:
            deleted = sheets.delete_last_op()
        except Exception as e:
            await q.edit_message_text(f"Ошибка удаления: {e}")
            return
        if not deleted:
            await q.edit_message_text("Уже нечего удалять.")
            return
        try:
            storage.push_deleted(deleted["direction"], deleted["qty"], deleted["my_rate"],
                                 deleted["google_rate"], deleted["date"], deleted["client"])
        except Exception as e:
            log.warning("push_deleted failed: %s", e)
        ils_bal, rub_bal = sheets.read_balances()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Восстановить", callback_data="undo:restore")]])
        await q.edit_message_text(
            f"Удалил строку:\n• {_fmt_row(deleted)}\n\n"
            f"Стало: {ils_bal:,.0f} ₪ / {rub_bal:,.0f} ₽\n\n"
            "Передумал — нажми «Восстановить» (или /restore).",
            reply_markup=kb)
        return

    if action == "restore":
        await _do_restore(q.edit_message_text)
        return


async def _do_restore(reply):
    """Восстановить последнюю удалённую строку (общий код для кнопки и /restore)."""
    d = storage.peek_deleted()
    if not d:
        await reply("Нет недавно удалённых строк для восстановления.")
        return
    try:
        sheets.append_operation(d["direction"], d["qty"], d["my_rate"],
                                d["google_rate"], d["date"], d["client"])
    except Exception as e:
        await reply(f"Ошибка восстановления: {e}")
        return
    storage.mark_restored(d["id"])
    ils_bal, rub_bal = sheets.read_balances()
    await reply(f"Восстановил строку:\n• {_fmt_row(d)}\n\n"
                f"Стало: {ils_bal:,.0f} ₪ / {rub_bal:,.0f} ₽")


async def cmd_restore(update, context):
    if not ok(update):
        await deny(update)
        return
    if not can_edit(update):
        await deny_edit(update)
        return
    await _do_restore(update.message.reply_text)


async def cmd_users(update, context):
    if not is_owner(update):
        await deny(update)
        return
    rows = storage.list_trusted()
    lines = ["Доступ к боту (помимо тебя — владельца):"]
    if not rows:
        lines.append("  пока никого")
    for r in rows:
        lines.append(f"  [{r.get('role') or 'editor'}] {r.get('name') or r['chat_id']} — id {r['chat_id']}")
    lines.append("\nУбрать доступ: /kick <id>")
    await update.message.reply_text("\n".join(lines))


async def cmd_kick(update, context):
    if not is_owner(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text("Укажи id: /kick 123456789")
        return
    try:
        cid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id должен быть числом.")
        return
    n = storage.remove_trusted(cid)
    await update.message.reply_text(f"Доступ для {cid} убран." if n else f"{cid} не найден.")


# ---------- разбор + подтверждение операции перед записью ----------

def _f(v):
    """Число из значения LLM (терпит запятую/пробелы); None если пусто."""
    if v in (None, ""):
        return None
    return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))


def _coerce_op(p, date_disp):
    """Распарсенное сообщение -> нормализованная операция или ValueError с причиной."""
    kind = p.get("kind")
    if kind == "trade":
        side = ledger.norm_side(p.get("side"))
        if side not in ("buy", "sell"):
            raise ValueError("не понял — покупка или продажа")
        qty, my, gg = _f(p.get("qty")), _f(p.get("my_rate")), _f(p.get("google_rate"))
        if not qty or qty <= 0 or my is None or gg is None:
            raise ValueError("не хватает данных (кол-во / мой курс / курс гугл)")
        return {"kind": "trade", "direction": "Продал" if side == "sell" else "Купил",
                "qty": qty, "my": my, "gg": gg, "client": (p.get("client") or "").strip(),
                "date": date_disp}
    if kind == "income":
        qty = _f(p.get("qty"))
        if not qty or qty <= 0:
            raise ValueError("не понял сумму шекелей")
        return {"kind": "income", "qty": qty, "note": (p.get("note") or "").strip(), "date": date_disp}
    if kind == "deposit":
        rub = _f(p.get("rub"))
        if not rub or rub <= 0:
            raise ValueError("не понял сумму рублей")
        return {"kind": "deposit", "rub": rub, "note": (p.get("note") or "").strip(), "date": date_disp}
    raise ValueError("это не операция")


def _preview_op(op):
    if op["kind"] == "trade":
        c = ", " + op["client"] if op["client"] else ""
        return f"{op['direction']} {op['qty']:,.0f} ₪ по {op['my']} (гугл {op['gg']}){c}, {op['date']}"
    if op["kind"] == "income":
        n = ", " + op["note"] if op["note"] else ""
        return f"Влил {op['qty']:,.0f} ₪ (партнёру){n}, {op['date']}"
    if op["kind"] == "deposit":
        n = ", " + op["note"] if op["note"] else ""
        return f"Влил {op['rub']:,.0f} ₽{n}, {op['date']}"
    return "?"


async def _apply_op(op, reply):
    """Записать подтверждённую операцию в таблицу и ответить результатом."""
    if op["kind"] == "trade":
        res = sheets.append_operation(op["direction"], op["qty"], op["my"], op["gg"], op["date"], op["client"])
        c = ", " + op["client"] if op["client"] else ""
        await reply(f"✓ {op['direction']} {op['qty']:,.0f} ₪ по {op['my']} (гугл {op['gg']}), "
                    f"доход {res['profit'] or 0:,.0f} ₽{c}\n"
                    f"Стало: {res['ils']:,.0f} ₪ у партнёра / {res['rub']:,.0f} ₽ у тебя\n/undo — удалить")
    elif op["kind"] == "income":
        res = sheets.append_operation("влил шекели", op["qty"], None, None, op["date"], op["note"])
        await reply(f"✓ Влил {op['qty']:,.0f} ₪ (партнёру)\n"
                    f"Стало: {res['ils']:,.0f} ₪ / {res['rub']:,.0f} ₽\n/undo — удалить")
    elif op["kind"] == "deposit":
        res = sheets.append_operation("влил рубли", op["rub"], None, None, op["date"], op["note"])
        await reply(f"✓ Влил {op['rub']:,.0f} ₽\n"
                    f"Стало: {res['ils']:,.0f} ₪ / {res['rub']:,.0f} ₽\n/undo — удалить")


async def on_tx_callback(update, context):
    """Инлайн-подтверждение записи операции."""
    q = update.callback_query
    await q.answer()
    if not can_edit(update):
        await q.edit_message_text("У тебя доступ только на просмотр — менять нельзя.")
        return
    action = (q.data or "tx:").split(":", 1)[1]
    op = context.user_data.pop("pending", None)
    if action == "cancel":
        await q.edit_message_text("Отменил, ничего не записал.")
        return
    if action == "save":
        if not op:
            await q.edit_message_text("Нечего записывать — запрос устарел, введи операцию заново.")
            return
        try:
            await _apply_op(op, q.edit_message_text)
        except Exception as e:
            await q.edit_message_text(f"Не смог записать ({e}). Попробуй ещё раз.")


# ---------- свободный текст: сделка / доход / пополнение / вопрос ----------

async def on_text(update, context):
    text = update.message.text.strip()
    if not ok(update):
        # незнакомец: принимаем только пароль (редактора или просмотра)
        u = update.effective_user
        ep, vp = access_password(), viewer_password()
        if ep and text == ep:
            storage.add_trusted(u.id, u.full_name or u.username or str(u.id), "editor")
            log.info("editor added: %s", u.id)
            await update.message.reply_text(
                "Пароль верный — полный доступ (просмотр + ввод).\n/start для меню.", reply_markup=KB)
        elif vp and text == vp:
            storage.add_trusted(u.id, u.full_name or u.username or str(u.id), "viewer")
            log.info("viewer added: %s", u.id)
            await update.message.reply_text(
                "Пароль верный — доступ только на ПРОСМОТР (без изменений).\n/start для меню.")
        else:
            await deny(update)
        return
    if text in _BTN:
        fn = {"now": cmd_now, "balance": cmd_balance, "forecast": cmd_forecast,
              "stats": cmd_stats, "history": cmd_history, "status": cmd_status,
              "undo": cmd_undo}[_BTN[text]]
        await fn(update, context)
        return
    try:
        p = llm.parse_message(text, today_iso())
    except Exception as e:
        await update.message.reply_text(f"Не смог разобрать (LLM): {e}")
        return

    kind = p.get("kind")
    date = p.get("date") or today_iso()
    if date in ("today", "сегодня"):
        date = today_iso()
    dt = _date_disp(date)

    if kind in ("trade", "income", "deposit"):
        if not can_edit(update):
            await deny_edit(update)
            return
        try:
            op = _coerce_op(p, dt)
        except ValueError as e:
            await update.message.reply_text(
                f"Похоже на операцию, но {e}. Напиши полнее, напр.: "
                f"«продал 5000 по 26.2 гугл 24.9 Игорь».")
            return
        # ПОДТВЕРЖДЕНИЕ перед записью — ловим ошибки распознавания до таблицы
        context.user_data["pending"] = op
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Записать", callback_data="tx:save"),
            InlineKeyboardButton("✗ Отмена", callback_data="tx:cancel")]])
        await update.message.reply_text(
            "Понял так:\n• " + _preview_op(op) + "\n\nЗаписывать?", reply_markup=kb)
        return

    # вопрос к статистике/истории
    try:
        await update.message.reply_text(llm.answer(text, build_qa_context()))
    except Exception as e:
        await update.message.reply_text(f"Не смог ответить ({e}).")


def build_qa_context():
    ops = sheets.read_ops()
    ils_bal, rub_bal = sheets.read_balances()
    trades = [o for o in ops if o["side"] in ("buy", "sell")]
    profit = sum(o["profit"] or 0 for o in trades)
    from collections import defaultdict
    agg = defaultdict(float)
    for o in trades:
        agg[_month_key(o["date"])] += o["profit"] or 0
    lines = [f"Сейчас: шекели у партнёра {ils_bal:,.0f} ₪, рубли {rub_bal:,.0f} ₽, "
             f"доход всего {profit:,.0f} ₽ ({len(trades)} сделок)."]
    lines.append("Доход по месяцам: " + "; ".join(f"{m}: {agg[m]:,.0f}₽" for m in sorted(agg)))
    lines.append("Последние операции (старые->новые):")
    for o in ops[-15:]:
        lines.append(f"  {o['date']} {o['A']} {o['qty'] or 0:,.0f} my={o['my']} g={o['gg']} "
                     f"доход={o['profit'] or 0:,.0f}₽ {o['client']}")
    return "\n".join(lines)


# ---------- рассылка ----------

async def job_daily(context):
    try:
        snap = data.current_snapshot()
        ils_bal, rub_bal = sheets.read_balances()
        for cid in ALLOWED:
            await context.bot.send_message(
                cid, f"Доброе утро. ILS/RUB {snap['ils_rub']:.3f} руб/шек. "
                     f"Баланс: {ils_bal:,.0f} ₪ / {rub_bal:,.0f} ₽.")
    except Exception as e:
        log.warning("daily job failed: %s", e)


async def job_weekly(context):
    try:
        text = build_forecast_text()
        for cid in ALLOWED:
            await context.bot.send_message(cid, "Прогноз на неделю:\n\n" + text)
    except Exception as e:
        log.warning("weekly job failed: %s", e)


async def job_eval_forecasts(context):
    """Ежедневно сверяем созревшие ИИ-прогнозы с фактом (обучение на результатах)."""
    try:
        storage.init()
        n = ai_forecast.evaluate_due()
        if n:
            log.info("evaluated %d matured AI forecasts", n)
    except Exception as e:
        log.warning("eval forecasts job failed: %s", e)


# ---------- бэкапы (планировщик — сам бот) ----------

def _spawn_backup(script):
    try:
        subprocess.Popen([sys.executable, os.path.join(str(config.BASE_DIR), "tools", script)],
                         env={**os.environ, "PYTHONPATH": str(config.BASE_DIR),
                              "PYTHONIOENCODING": "utf-8"},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("backup spawned: %s", script)
    except Exception as e:
        log.warning("backup spawn failed %s: %s", script, e)


def _backups_tick():
    """Ежедневно: лог. Недельный — если свежего нет за 6.5 дней (catch-up при простое ПК)."""
    _spawn_backup("backup_daily.py")
    wk = glob.glob(os.path.join(str(config.BASE_DIR), "backups", "weekly", "shekel-backup-*"))
    newest = max((os.path.getmtime(f) for f in wk), default=0)
    if time.time() - newest > 6.5 * 86400:
        _spawn_backup("backup_weekly.py")


async def job_backups(context):
    _backups_tick()


# ---------- сторож связи (самовосстановление) ----------
# Где Telegram заблокирован, доступ идёт через прокси, а прокси иногда рвёт long-poll-
# соединение. PTB при этом НЕ падает — молча ретраит, и бот выглядит «немым».
# Сторож активно пингует Telegram (get_me); если связь мертва несколько раз
# подряд — перезапускает процесс (run_bot.bat поднимет заново с чистым пулом).
_HB = {"fails": 0}
HEARTBEAT_EVERY = 30          # сек между проверками
HEARTBEAT_MAX_FAILS = 5       # столько подряд = перезапуск (~2.5 мин глухоты)


async def job_heartbeat(context):
    try:
        await context.bot.get_me()
        if _HB["fails"]:
            log.info("связь с Telegram восстановлена (после %d сбоев)", _HB["fails"])
        _HB["fails"] = 0
    except Exception as e:
        _HB["fails"] += 1
        log.warning("heartbeat: нет связи с Telegram (#%d): %s", _HB["fails"], str(e)[:90])
        if _HB["fails"] >= HEARTBEAT_MAX_FAILS:
            log.error("heartbeat: Telegram недоступен %d раз подряд -> перезапуск процесса",
                      _HB["fails"])
            os._exit(1)       # run_bot.bat перезапустит с чистым состоянием


# ---------- single-instance lock (защита от Telegram Conflict) ----------
_LOCK_SOCK = None


def _acquire_single_instance(port=49517):
    """True, если мы единственный инстанс. Держит сокет до конца жизни процесса.
    Два бота на один токен = Telegram Conflict (getUpdates выбивает друг друга)."""
    global _LOCK_SOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _LOCK_SOCK = s
        return True
    except OSError:
        s.close()
        return False


def _direct_telegram_ok(timeout=6):
    """Доступен ли api.telegram.org НАПРЯМУЮ (без прокси). Лёгкий GET без токена."""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # игнор системного прокси
        op.open("https://api.telegram.org/", timeout=timeout)
        return True
    except Exception:
        return False


def resolve_proxy():
    """Какой прокси использовать для Telegram (None = напрямую).
    Авто-режим: где Telegram открыт → напрямую (прокси игнорируем);
    где прямой доступ заблокирован → через config.TG_PROXY."""
    proxy = (config.TG_PROXY or "").strip()
    if not proxy:
        return None                       # прокси не задан → только напрямую
    if config.TG_PROXY_AUTO and _direct_telegram_ok():
        log.info("Telegram доступен напрямую → прокси НЕ используется")
        return None
    return proxy


def _make_request(proxy):
    """httpx-пул под медленный/рвущийся прокси:
    - щедрые таймауты (long-poll 25с + ~2с задержка прокси);
    - HTTP/1.1 (HTTP/2-мультиплексирование через CONNECT-прокси даёт сбои);
    - БЕЗ keep-alive (max_keepalive_connections=0): каждое обращение — свежее
      соединение. Иначе httpx переиспользует «протухший» туннель прокси и ловит
      SSL bad-record-mac / RemoteProtocolError на long-poll (главная причина
      «бот иногда молчит»)."""
    return HTTPXRequest(
        proxy=proxy or None,
        connect_timeout=20.0, read_timeout=45.0,
        write_timeout=20.0, pool_timeout=20.0,
        http_version="1.1",
        httpx_kwargs={"limits": httpx.Limits(max_connections=8,
                                             max_keepalive_connections=0,
                                             keepalive_expiry=0.0)})


def main():
    if not _acquire_single_instance():
        log.error("другой инстанс бота уже запущен -> выходим (избегаем Telegram Conflict)")
        sys.exit(3)   # код 3 = «уже запущен»: run_bot.bat по нему НЕ перезапускает (без зомби-циклов)
    try:
        storage.init()   # rates/trusted_users/deleted_ops в локальном SQLite
    except Exception as e:
        log.warning("storage.init failed (continuing): %s", e)
    # выбираем direct/proxy один раз на старте; при смене сети сторож перезапустит
    proxy = resolve_proxy()
    global _STARTED, _CONN
    _STARTED = datetime.datetime.now()
    _CONN = ("через прокси " + proxy) if proxy else "напрямую"
    builder = (Application.builder().token(config.TG_TOKEN)
               .request(_make_request(proxy))
               .get_updates_request(_make_request(proxy)))
    log.info("Telegram: %s", ("через прокси " + proxy) if proxy else "напрямую")
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("positions", cmd_balance))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CallbackQueryHandler(on_undo_callback, pattern=r"^undo:"))
    app.add_handler(CallbackQueryHandler(on_tx_callback, pattern=r"^tx:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    if jq is not None:
        try:
            jq.run_repeating(job_heartbeat, interval=HEARTBEAT_EVERY, first=HEARTBEAT_EVERY)
            jq.run_daily(job_eval_forecasts, time=datetime.time(9, 30))   # сверка прогнозов с фактом
            jq.run_daily(job_daily, time=datetime.time(9, 0))
            jq.run_daily(job_weekly, time=datetime.time(9, 0), days=(0,))  # день/час уточним
            jq.run_daily(job_backups, time=datetime.time(23, 50))          # лог + недельный если пора
            jq.run_once(job_backups, when=25)                             # catch-up при старте
        except Exception as e:
            log.warning("scheduling failed (continuing without jobs): %s", e)
    log.info("bot starting, allowed=%s", ALLOWED)
    # timeout=25 — длина long-poll; read_timeout(45) с запасом перекрывает её даже
    # при ~2с задержке прокси. drop_pending_updates=False — не терять сообщения,
    # пришедшие пока бот переподключался.
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=25,
                    drop_pending_updates=False)


if __name__ == "__main__":
    main()
