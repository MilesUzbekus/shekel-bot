"""Обёртка OpenAI: чат-комплишены для парсинга сообщений, анализа и Q&A.
Ключ берётся из config (vault), в логи/чат не печатается.
"""
import json
import urllib.request
import urllib.error

from src import config

API = "https://api.openai.com/v1/chat/completions"


def chat(messages, model=None, max_tokens=700, temperature=0.3, json_mode=False, timeout=60):
    model = model or config.OPENAI_MODEL
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + config.OPENAI_KEY,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"OpenAI {e.code}: {body}")


PARSE_SYS = """Ты — парсер сообщений для личного учёта валютных сделок (шекель ILS / рубль RUB).
Классифицируй сообщение пользователя строго в JSON. Поле kind — одно из:
- "trade": сделка. Поля: side ("buy"=купил / "sell"=продал), qty (кол-во шекелей, число),
  my_rate (мой курс, руб за шекель), google_rate (курс гугл, руб за шекель),
  client (имя/аккаунт клиента, строка; "" если нет), date ("YYYY-MM-DD"; если не сказано — today).
- "income": вливание ШЕКЕЛЕЙ в бизнес вне сделки (партнёр получил / влили шекели как капитал).
  Поля: qty (шекели, число), date, note (строка, напр. "партнёр получил").
- "deposit": вливание РУБЛЕЙ в бизнес (пополнение капитала рублями). Поля: rub (число), date, note (строка).
- "question": вопрос к статистике/истории (всё, что не операция).
today = {today}. Отвечай ТОЛЬКО валидным JSON, без пояснений."""


def parse_message(text, today):
    raw = chat([{"role": "system", "content": PARSE_SYS.replace("{today}", today)},
                {"role": "user", "content": text}],
               model=config.OPENAI_MODEL, json_mode=True, max_tokens=300, temperature=0)
    try:
        return json.loads(raw)
    except Exception:
        return {"kind": "question"}


ANSWER_SYS = """Ты — аналитик личного валютного бизнеса (шекель/рубль) пользователя.
Отвечай кратко, по делу, на русском, опираясь ТОЛЬКО на данные в контексте.
Если данных не хватает — скажи прямо. Не выдумывай цифры."""


def answer(question, context):
    return chat([{"role": "system", "content": ANSWER_SYS},
                 {"role": "user", "content": f"Данные:\n{context}\n\nВопрос: {question}"}],
                model=config.OPENAI_MODEL_SMART, max_tokens=500, temperature=0.3)
