"""Выполнение SQL в Supabase через Management API (HTTPS:443).
Канал стабилен на линии РФ->AWS (в отличие от Postgres-пулера), поэтому все
ЗАПИСИ синка идут сюда одной транзакцией. Значения не печатаются.
"""
import json
import urllib.request
import urllib.error

from src import config

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def run_sql(sql, timeout=120):
    req = urllib.request.Request(
        "https://api.supabase.com/v1/projects/" + config.SUPABASE_REF + "/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + config.vault_secret("supabase_token"),
                 "User-Agent": _UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def lit(v):
    """Python -> SQL-литерал (экранирование апострофов)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"
