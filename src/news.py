"""Сбор свежих новостей для ИИ-прогноза (бесплатно, по RSS).

Тянем заголовки из лент (макроэкономика, нефть, экономика РФ/ЦБ), фильтруем по свежести,
отдаём компактным списком для подачи в LLM. Битые/недоступные ленты молча
пропускаем — прогноз должен строиться даже если часть лент недоступна.
"""
import calendar
import datetime as dt
import time

import requests
import feedparser

from src import config

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}


def _source_name(url):
    try:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        return host.replace("www.", "")
    except Exception:
        return url


def _entry_dt(e):
    """Дата записи как date (или None)."""
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            try:
                return dt.date.fromtimestamp(calendar.timegm(t))
            except Exception:
                pass
    return None


def fetch_headlines(feeds=None, max_age_days=None, max_items=None, per_feed_timeout=12):
    """Список свежих заголовков: [{'date','source','title'}], новые->старые."""
    feeds = feeds or config.NEWS_FEEDS
    max_age_days = max_age_days if max_age_days is not None else config.NEWS_MAX_AGE_DAYS
    max_items = max_items or config.NEWS_MAX_ITEMS
    today = dt.date.today()
    out = []
    sources_ok = []
    for url in feeds:
        try:
            r = requests.get(url, headers=UA, timeout=per_feed_timeout)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            src = _source_name(url)
            got = 0
            for e in parsed.entries:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                d = _entry_dt(e)
                if d is not None and (today - d).days > max_age_days:
                    continue
                out.append({"date": d.isoformat() if d else "", "source": src, "title": title})
                got += 1
            if got:
                sources_ok.append(src)
        except Exception:
            continue
    # новые сверху; без даты — в конец
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:max_items], sources_ok


def headlines_text(items):
    """Компактный текст для подачи в LLM."""
    if not items:
        return "(свежих новостей собрать не удалось)"
    return "\n".join(f"- [{it['date'] or '?'}] {it['source']}: {it['title']}" for it in items)
