"""Общий стенд pytest. Пока житель один — фикстура `monkey_prices`.

Зачем она: `tests/test_paybot_deeplink.py` писался как файл, который запускают
руками (`python tests/test_paybot_deeplink.py`), и стенд ему готовит собственный
`main()`: SQLite вместо боевой базы, молчащие уведомления Николь, подменённые
цены кассы. Под pytest `main()` не вызывается, поэтому три теста, которые ждут
аргумент `monkey_prices`, падали на setup: «fixture not found» — 3 ошибки в
каждом прогоне, шум, за которым не видно настоящих поломок.

Фикстура повторяет ровно тот же стенд и возвращает подменялку цен. Всё, что
она трогает, возвращается на место после теста: файл-тест остаётся годным и для
запуска руками, и импорт `app.paybot` в сеть не ходит (PAY_BOT_TOKEN пуст).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def monkey_prices():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import paybot as pb
    from app.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    saved_session, saved_notify = pb.SessionLocal, pb._notify_admin
    saved_prices = pb.lava.offer_prices
    pb.SessionLocal = sessionmaker(bind=engine)

    async def _no_admin(_text):        # уведомления Николь наружу не уходят
        return None

    pb._notify_admin = _no_admin

    def _set(prices):
        pb.lava.offer_prices = lambda offer_id=None: dict(prices)

    try:
        yield _set
    finally:
        pb.SessionLocal, pb._notify_admin = saved_session, saved_notify
        pb.lava.offer_prices = saved_prices
