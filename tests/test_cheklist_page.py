# -*- coding: utf-8 -*-
"""Страница чек-листа: отдаётся, кнопки ведут в бота заявкой, счёт перехода пишется."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET", "test-secret-not-the-default-value-000")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://t:t@localhost:5432/t")
os.environ["PAY_BOT_TOKEN"] = ""

from fastapi.testclient import TestClient
from app.main import app
from app import linkstat

записано = []
linkstat.record_click = lambda *a, **k: записано.append(a)

c = TestClient(app)
r = c.get("/cheklist/ai-sotrudnik")
ok = True


def check(cond, what):
    global ok
    print(("  ok      " if cond else "  ПАДАЕТ  ") + what)
    if not cond:
        ok = False


check(r.status_code == 200, "страница отдаётся")
t = r.text
check("AI-сотрудник" in t, "заголовок на месте")
check(t.count("start=zayavka-cheklist") == 5, "пять кнопок ведут в бота заявкой, а не на /assistant")
check("oncount.co/assistant?utm_source=cheklist" not in t, "старых ссылок на лендинг не осталось")
check("Даша" in t and "Сергей" in t, "оба отзыва на месте")
check("20 €" in t, "цена интенсива в евро")
check(len(записано) == 1, "переход записан один раз")
print("ВСЁ ЗЕЛЁНОЕ" if ok else "ЕСТЬ ПАДЕНИЯ")
sys.exit(0 if ok else 1)
