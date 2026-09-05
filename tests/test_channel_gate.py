"""Привратник канала: статус пишется тому человеку, метка рассылки не теряется.

Отчёт рассылки в ARDORIUM собирается из двух полей одной таблицы — `status` и
`source`, — и обе цифры до 05.09.2026 умели врать молча:

1. кик или одобрение заявки РУКАМИ приходят от админа, а строку искали по
   `from_user` — то есть по исполнителю действия. `left` уезжал на строку
   Николь, а вышедший оставался `in_channel` навсегда;
2. человек, кликнувший ссылку рассылки (`dl:<код>`) и потом постучавшийся в
   канал, получал поверх метки безымянный `join_request` — и выпадал из отчёта
   той самой рассылки, которая за него заплатила.

Ни то, ни другое не видно ни по одной ошибке в журнале: цифры просто другие.
Поэтому стережём поимённо.

БД — in-memory SQLite, `channel_gate.SessionLocal` подменён на неё. Сети нет:
Telegram здесь — четыре простых класса, бот собирает вызовы в списки.

Запуск:  python tests/test_channel_gate.py   |   pytest tests/test_channel_gate.py
"""
import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "test-secret-not-the-default-value-000")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://t:t@localhost:5432/t")
# Пустой токен: Bot() не создаётся, в сеть модуль не ходит.
os.environ["PAY_BOT_TOKEN"] = ""

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from aiogram.dispatcher.event.bases import SkipHandler  # noqa: E402

from app import channel_config as T  # noqa: E402
from app import channel_gate  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Base, ChannelSubscriber  # noqa: E402

CHANNEL = "-100777"          # наш канал на стенде
CHUZHOY = "-100999"          # чей-то ещё: события оттуда не наши
КОД = "dl:k7m2xqp"           # метка рассылки: 7 знаков из алфавита ARDORIUM


def _sub(telegram_id, *, status="asked", source=None, confirmed=False,
         pending=False):
    return ChannelSubscriber(
        telegram_id=telegram_id, username=f"user{telegram_id}",
        first_name="Кто-то", status=status, source=source,
        pending_request=pending, created_at=datetime(2026, 9, 1, 10, 0),
        age_confirmed_at=datetime(2026, 9, 1, 10, 5) if confirmed else None,
    )


@contextmanager
def _stand(*subs):
    """Стенд привратника: своя SQLite и свой канал.

    Прежние значения возвращаем в `finally` ОБА. Оба глобальные: pytest гоняет
    все файлы в одном процессе, и стенд, не убравший за собой, увёл бы соседние
    тесты в чужую базу или в чужой канал — молча и не там, где сломался.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    saved_sm, saved_cid = channel_gate.SessionLocal, settings.NIKOL_CHANNEL_ID
    channel_gate.SessionLocal = maker
    settings.NIKOL_CHANNEL_ID = CHANNEL
    # Строки кладём и СРАЗУ отпускаем соединение: у in-memory SQLite оно одно на
    # поток, и открытая сессия делила бы транзакцию с сессиями обработчика.
    seed = maker()
    seed.add_all(subs)
    seed.commit()
    seed.close()
    try:
        yield maker
    finally:
        channel_gate.SessionLocal = saved_sm
        settings.NIKOL_CHANNEL_ID = saved_cid


def _row(maker, telegram_id):
    """Строка из базы свежей сессией: читаем то, что записал обработчик."""
    with maker() as s:
        return s.query(ChannelSubscriber).filter_by(telegram_id=telegram_id).one()


def _count(maker):
    with maker() as s:
        return s.query(ChannelSubscriber).count()


# ─── Telegram на стенде: ни одного сетевого вызова ───────────────────────────

class FakeUser:
    def __init__(self, id, username="someone", first_name="Кто-то"):  # noqa: A002
        self.id, self.username, self.first_name = id, username, first_name


class FakeChat:
    def __init__(self, id, type="channel"):  # noqa: A002
        self.id, self.type = id, type


class FakeChatMember:
    """`ChatMemberMember` / `ChatMemberLeft` / `ChatMemberBanned`: у всех троих
    есть и `status`, и `user` — сам человек, чьё членство изменилось."""

    def __init__(self, status, user):
        self.status, self.user = status, user


class FakeChatMemberUpdated:
    def __init__(self, chat, from_user, new_chat_member):
        self.chat, self.from_user = chat, from_user
        self.new_chat_member = new_chat_member


class FakeInviteLink:
    def __init__(self, name):
        self.name = name


class FakeChatJoinRequest:
    def __init__(self, chat, from_user, user_chat_id, invite_link=None):
        self.chat, self.from_user = chat, from_user
        self.user_chat_id, self.invite_link = user_chat_id, invite_link


class FakeBot:
    def __init__(self):
        self.sent, self.approved = [], []

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))

    async def approve_chat_join_request(self, **kw):
        self.approved.append(kw)


def _texts(bot):
    return [a[1] if len(a) > 1 else kw.get("text") for a, kw in bot.sent]


def _member_event(status, *, who_id, by_id, chat_id=CHANNEL):
    """Событие chat_member: действие сделал `by_id`, изменилось членство `who_id`."""
    return FakeChatMemberUpdated(
        chat=FakeChat(int(chat_id)),
        from_user=FakeUser(by_id, username="nikol"),
        new_chat_member=FakeChatMember(status, FakeUser(who_id)),
    )


# ─── (а) статус пишется тому, чьё членство изменилось ────────────────────────

def test_kick_by_admin_marks_the_kicked_not_the_admin():
    # Николь кикает человека руками. `from_user` — она, `new_chat_member.user` —
    # он. Пока искали по `from_user`, «вышел» записывалось ЕЙ.
    with _stand(_sub(1, status="in_channel", source="deeplink"),
                _sub(2, status="invited", source=КОД)) as maker:
        asyncio.run(channel_gate.on_channel_member(
            _member_event("kicked", who_id=2, by_id=1)))
        assert _row(maker, 2).status == "left", "выход записан не тому"
        assert _row(maker, 1).status == "in_channel", "админа выкинуло из канала"


def test_manual_approve_marks_the_added_not_the_admin():
    # Обратная сторона того же: заявку одобрили руками из интерфейса Telegram.
    with _stand(_sub(1, status="in_channel", source="deeplink"),
                _sub(2, status="invited", source=КОД)) as maker:
        asyncio.run(channel_gate.on_channel_member(
            _member_event("member", who_id=2, by_id=1)))
        assert _row(maker, 2).status == "in_channel", "вход записан не тому"
        assert _row(maker, 1).status == "in_channel"


def test_unknown_person_creates_no_row():
    # Таблица про подтверждения возраста, а не про всех подписчиков канала:
    # вошедший мимо привратника строки не заводит.
    with _stand(_sub(1, status="in_channel"), _sub(2, status="invited")) as maker:
        asyncio.run(channel_gate.on_channel_member(
            _member_event("member", who_id=3, by_id=1)))
        assert _count(maker) == 2, "завели строку тому, кого не спрашивали"


def test_foreign_channel_is_passed_on():
    # Клубный канал ждёт club.on_channel_member: не «не наш — забыли», а
    # «не наш — отдай следующему». Иначе вход в клуб молча пропадает.
    with _stand(_sub(1, status="in_channel"), _sub(2, status="invited")) as maker:
        with pytest.raises(SkipHandler):
            asyncio.run(channel_gate.on_channel_member(
                _member_event("kicked", who_id=2, by_id=1, chat_id=CHUZHOY)))
        assert _row(maker, 1).status == "in_channel"
        assert _row(maker, 2).status == "invited", "чужой канал тронул нашу строку"


# ─── (б) метка рассылки: побеждает первое касание ────────────────────────────

def test_mailing_tag_survives_a_join_request():
    # Главный случай ради которого всё: кликнул в рассылке, потом постучался в
    # канал. Метка `dl:` обязана остаться — по ней ARDORIUM и считает отчёт.
    with _stand(_sub(777, source=КОД)) as maker:
        bot = FakeBot()
        asyncio.run(channel_gate.ask_age(bot, 777, FakeUser(777), "join_request"))
        assert _row(maker, 777).source == КОД, "метку рассылки затёрли заявкой"
        assert T.AGE_ASK in _texts(bot), "вопрос про 18+ не задан"


def test_general_tag_gives_way_to_the_mailing_one():
    # Обратный порядок: сперва просто открыл бота, потом пришёл по рассылке.
    # Общей метке уступать нечего, `deeplink` меняется на код.
    with _stand(_sub(777, source="deeplink")) as maker:
        asyncio.run(channel_gate.ask_age(FakeBot(), 777, FakeUser(777), КОД))
        assert _row(maker, 777).source == КОД


def test_empty_source_is_filled():
    # Строки нет вовсе — её заводит _sub, и метка пишется как есть.
    with _stand() as maker:
        asyncio.run(channel_gate.ask_age(FakeBot(), 777, FakeUser(777),
                                         "join_request"))
        assert _row(maker, 777).source == "join_request"
        assert _row(maker, 777).status == "asked"


def test_channel_command_does_not_wipe_the_tag():
    # `/channel` шлёт «deeplink» всегда (paybot.py:651) — это «вернулся, ссылка
    # истекла», а не новый источник. Раньше он стирал метку рассылки у всех,
    # кто хоть раз воспользовался командой.
    with _stand(_sub(777, source=КОД)) as maker:
        asyncio.run(channel_gate.ask_age(FakeBot(), 777, FakeUser(777), "deeplink"))
        assert _row(maker, 777).source == КОД


def test_first_touch_wins_between_two_concrete_tags():
    # Две конкретные метки: `dl:` из рассылки против `jr:agents` с именной
    # ссылки. Оставляем ПЕРВУЮ — ARDORIUM сверяет по коду, который сам отправил.
    with _stand(_sub(777, source=КОД)) as maker:
        asyncio.run(channel_gate.ask_age(FakeBot(), 777, FakeUser(777), "jr:agents"))
        assert _row(maker, 777).source == КОД
    # И два разных кода рассылки между собой — тоже: побеждает первый.
    with _stand(_sub(778, source=КОД)) as maker:
        asyncio.run(channel_gate.ask_age(FakeBot(), 778, FakeUser(778), "dl:b4n9wzr"))
        assert _row(maker, 778).source == КОД


def test_join_request_keeps_the_tag_end_to_end():
    # Целиком путь неподтвердившего: заявка → метка цела → вопрос в личку.
    # Метку тут пишут ДВАЖДЫ — сам on_join_request и ask_age внутри него.
    with _stand(_sub(777, source=КОД)) as maker:
        bot = FakeBot()
        ev = FakeChatJoinRequest(chat=FakeChat(int(CHANNEL)),
                                 from_user=FakeUser(777), user_chat_id=777)
        asyncio.run(channel_gate.on_join_request(ev, bot))
        row = _row(maker, 777)
        assert row.source == КОД, "метку затёрли по дороге заявки"
        assert row.pending_request is True, "заявка не отмечена как висящая"
        assert T.AGE_ASK in _texts(bot)


def test_join_request_of_a_confirmed_person_keeps_the_tag():
    # Подтвердившему возраст вопрос не задают — ему сразу открывают канал через
    # grant_access. Метка обязана пережить и эту ветку, где ask_age не зовётся.
    with _stand(_sub(777, source=КОД, status="invited", confirmed=True)) as maker:
        bot = FakeBot()
        ev = FakeChatJoinRequest(chat=FakeChat(int(CHANNEL)),
                                 from_user=FakeUser(777), user_chat_id=777,
                                 invite_link=FakeInviteLink("agents"))
        asyncio.run(channel_gate.on_join_request(ev, bot))
        row = _row(maker, 777)
        assert row.source == КОД, "именная ссылка затёрла метку рассылки"
        assert row.status == "in_channel", "заявку не одобрили"
        assert [kw["user_id"] for kw in bot.approved] == [777]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} тестов пройдено.")
