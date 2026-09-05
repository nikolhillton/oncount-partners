"""Счётчики по меткам рассылки наружу (план 2026-09-04).

Стерегут ровно то, чем эта дорога опасна: она отдаёт данные привратника канала
ЧУЖОМУ сервису. Значит, проверяем два разных страха.

1. Цифры не врут: человек считается один раз и в том статусе, в котором он
   сейчас; чужие источники (заявка из канала) в отчёт рассылки не попадают.
2. Наружу не уезжает ничего личного и никто лишний: без права — 404, а в теле
   ответа нет ни telegram_id, ни имени, ни username.

БД — in-memory SQLite, сессия подменена через dependency_overrides: сети и
живой базы тут нет. Стартовый хук приложения (create_all + seed) не трогаем —
TestClient без `with` его не запускает.

Запуск:  python tests/test_channel_tags.py   |   pytest tests/test_channel_tags.py
"""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "test-secret-not-the-default-value-000")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://t:t@localhost:5432/t")
# Пустой токен: бот оплат не поднимается, в сеть импорт приложения не ходит.
os.environ["PAY_BOT_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import channel_gate  # noqa: E402
from app import main as web  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, ChannelSubscriber  # noqa: E402

URL = "/admin/api/channel-tags"
TOKEN = "test-token-ardorium-0000000000"
NOW = datetime(2026, 9, 5, 12, 0, 0)


def _sub(telegram_id, source, status, *, days_ago=1, confirmed=False, invited=False,
         username="nikolina", first_name="Мария"):
    """Строка подписчика на стенде. Имя и username задаём НАРОЧНО: сторож ПД
    ищет их в ответе, а искать нечего, если в базе их не было."""
    born = NOW - timedelta(days=days_ago)
    return ChannelSubscriber(
        telegram_id=telegram_id, username=username, first_name=first_name,
        status=status, source=source, created_at=born,
        age_confirmed_at=born + timedelta(minutes=5) if confirmed else None,
        invited_at=born + timedelta(minutes=10) if invited else None,
        invite_link="https://t.me/+secret-invite" if invited else None,
    )


@contextmanager
def _stand(*subs, token=TOKEN):
    # Один общий коннект на все потоки: маршрут FastAPI выполняется в
    # threadpool, а in-memory SQLite живёт в том потоке, где создан.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(subs)
    session.commit()
    # Возвращаем прежнюю подмену, а не снимаем её: вложенный стенд иначе
    # оставлял бы внешний без сессии, и тот молча уходил бы в боевой Postgres.
    had = get_session in app.dependency_overrides
    saved_dep = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = lambda: session
    saved = settings.CHANNEL_TAGS_TOKEN
    settings.CHANNEL_TAGS_TOKEN = token
    # Лимит частоты живёт в процессе и общий на все тесты: адрес теперь под
    # потолком 30/60с, и без очистки соседние тесты складывались бы в один бакет.
    web._RL_HITS.clear()
    try:
        yield TestClient(app), session
    finally:
        settings.CHANNEL_TAGS_TOKEN = saved
        if had:
            app.dependency_overrides[get_session] = saved_dep
        else:
            app.dependency_overrides.pop(get_session, None)
        web._RL_HITS.clear()
        session.close()


def _rows(client, **params):
    r = client.get(URL, params=params, headers={"X-Api-Token": TOKEN})
    assert r.status_code == 200, r.text
    return {row["tag"]: row for row in r.json()["rows"]}


# ─── (а) считаем людей по меткам ─────────────────────────────────────────────

def test_counts_by_tag():
    with _stand(
        _sub(101, "dl:aaa1111", "in_channel", days_ago=3, confirmed=True, invited=True),
        _sub(102, "dl:aaa1111", "left", days_ago=2, confirmed=True, invited=True),
        _sub(103, "dl:bbb2222", "asked", days_ago=1),
        # Заявка прямо в канал: не рассылка, в отчёт по меткам попасть не должна.
        _sub(104, "join_request", "confirmed", days_ago=1),
    ) as (client, _):
        rows = _rows(client)
        assert set(rows) == {"dl:aaa1111", "dl:bbb2222"}, "чужой источник в отчёте"

        a = rows["dl:aaa1111"]
        assert (a["in_channel"], a["left"]) == (1, 1)
        assert (a["asked"], a["confirmed"], a["invited"], a["declined"]) == (0, 0, 0, 0), \
            "человек посчитан дважды: он всегда ровно в одном статусе"
        # first_seen — приход первого, last_seen — последнее движение (выдача ссылки).
        assert a["first_seen"] == "2026-09-02T12:00:00Z"
        assert a["last_seen"] == "2026-09-03T12:10:00Z"

        b = rows["dl:bbb2222"]
        assert b["asked"] == 1 and sum(b[s] for s in channel_gate.TAG_STATUSES) == 1
        # Ни одной даты не потеряли: без invited/confirmed берётся created_at.
        assert b["first_seen"] == b["last_seen"] == "2026-09-04T12:00:00Z"


def test_unknown_status_visible_in_total():
    # Привратник сегодня пишет ровно шесть статусов. Если завтра появится
    # седьмой, человек не попадёт ни в один счётчик — и без `total` пропал бы
    # молча, уменьшив цифры на карточке рассылки без единого признака.
    with _stand(
        _sub(801, "dl:aaa1111", "asked"),
        _sub(802, "dl:aaa1111", "banned"),      # статус не из шести
    ) as (client, _):
        row = _rows(client)["dl:aaa1111"]
        assert row["total"] == 2, "total считает людей, а не известные статусы"
        assert sum(row[s] for s in channel_gate.TAG_STATUSES) == 1
        assert row["total"] != sum(row[s] for s in channel_gate.TAG_STATUSES), \
            "расхождение обязано быть видно снаружи"


def test_total_matches_six_counters():
    # Обратная сторона того же: пока статусы известны, total = сумма шести.
    with _stand(
        _sub(811, "dl:aaa1111", "in_channel"), _sub(812, "dl:aaa1111", "left"),
        _sub(813, "dl:aaa1111", "declined"), _sub(814, "dl:bbb2222", "invited"),
    ) as (client, _):
        for row in _rows(client).values():
            assert row["total"] == sum(row[s] for s in channel_gate.TAG_STATUSES)


def test_rows_sorted_fresh_first():
    # Порядок задан явно: свежая рассылка сверху. Иначе карточка ARDORIUM
    # получала бы строки в порядке, который зависит от плана запроса.
    with _stand(
        _sub(201, "dl:staraya", "asked", days_ago=30),
        _sub(202, "dl:svezhaya", "asked", days_ago=1),
    ) as (client, _):
        r = client.get(URL, headers={"X-Api-Token": TOKEN})
        assert [row["tag"] for row in r.json()["rows"]] == ["dl:svezhaya", "dl:staraya"]


def test_no_subscribers_empty_rows():
    with _stand() as (client, _):
        body = client.get(URL, headers={"X-Api-Token": TOKEN}).json()
        assert body["rows"] == [] and body["generated_at"].endswith("Z")


# ─── (б) since ───────────────────────────────────────────────────────────────

def test_since_cuts_old():
    with _stand(
        _sub(301, "dl:staraya", "in_channel", days_ago=40),
        _sub(302, "dl:novaya", "asked", days_ago=1),
    ) as (client, _):
        assert set(_rows(client, since="2026-09-01")) == {"dl:novaya"}
        assert set(_rows(client)) == {"dl:staraya", "dl:novaya"}, "без since режем лишнее"


def test_response_echoes_applied_filter():
    # `first_seen` считается по попавшим в выборку: с `since` это «первый в
    # окне», а не «первый по метке». Одна и та же метка отдаёт две разные даты,
    # и различить их можно только по эху применённого фильтра.
    with _stand(
        _sub(321, "dl:ccc3333", "in_channel", days_ago=40),
        _sub(322, "dl:ccc3333", "asked", days_ago=1),
    ) as (client, _):
        full = client.get(URL, headers={"X-Api-Token": TOKEN}).json()
        win = client.get(URL, params={"since": "2026-09-01"},
                         headers={"X-Api-Token": TOKEN}).json()
        assert full["since"] is None and full["prefix"] == "dl:"
        assert win["since"] == "2026-09-01"
        assert full["rows"][0]["first_seen"] != win["rows"][0]["first_seen"], \
            "стенд обязан показывать обе даты, иначе сторож ничего не стережёт"
        # Эхо нормализованное: что применили, то и вернули.
        norm = client.get(URL, params={"since": "2026-9-1", "prefix": " jr: "},
                          headers={"X-Api-Token": TOKEN}).json()
        assert (norm["since"], norm["prefix"]) == ("2026-09-01", "jr:")


def test_since_broken_is_400_not_silence():
    # Молчаливое игнорирование кривой даты — худший исход: расписание годами
    # тянуло бы полную выборку и считало, что фильтрует.
    with _stand(_sub(311, "dl:aaa1111", "asked")) as (client, _):
        r = client.get(URL, params={"since": "05.09.2026"}, headers={"X-Api-Token": TOKEN})
        assert r.status_code == 400


# ─── (в) право двумя дверями ─────────────────────────────────────────────────

def test_access_two_doors():
    with _stand(_sub(401, "dl:aaa1111", "asked")) as (client, _):
        assert client.get(URL).status_code == 404, "аноним не должен видеть адрес"
        assert client.get(URL, headers={"X-Api-Token": "chuzhoy"}).status_code == 404
        # Токен с тем же началом: сравнение целиком, а не по префиксу.
        assert client.get(URL, headers={"X-Api-Token": TOKEN[:-1]}).status_code == 404
        assert client.get(URL, headers={"X-Api-Token": TOKEN}).status_code == 200


def test_empty_setting_closes_token_door():
    # Незаполненная переменная в Railway не должна открывать адрес тому, кто
    # прислал пустой заголовок (или не прислал его вовсе).
    with _stand(_sub(411, "dl:aaa1111", "asked"), token="") as (client, _):
        assert client.get(URL, headers={"X-Api-Token": ""}).status_code == 404
        assert client.get(URL).status_code == 404


def test_non_ascii_token_header_does_not_crash():
    # Заголовок приходит снаружи: compare_digest на строке с не-ASCII бросает
    # TypeError, и вместо 404 адрес отвечал бы 500, подтверждая, что он есть.
    with _stand(_sub(421, "dl:aaa1111", "asked")) as (client, _):
        r = client.get(URL, headers={"X-Api-Token": "токен".encode("utf-8")})
        assert r.status_code == 404


def test_cache_control_no_store():
    with _stand(_sub(431, "dl:aaa1111", "asked")) as (client, _):
        r = client.get(URL, headers={"X-Api-Token": TOKEN})
        assert r.headers.get("cache-control") == "no-store"


def test_token_brute_force_is_capped():
    # Подбор токена — единственный способ войти без cookie. Адрес обязан жить
    # под тем же потолком, что вход и приглашения (30 запросов с IP за минуту).
    with _stand(_sub(441, "dl:aaa1111", "asked")) as (client, _):
        chuzhoy = {"X-Forwarded-For": "203.0.113.7"}   # свой бакет, соседей не трогаем
        codes = [client.get(URL, headers={"X-Api-Token": "podbor-%d" % i, **chuzhoy}).status_code
                 for i in range(web._RL_MAX + 5)]
        assert 429 in codes, "подбор токена ничем не ограничен"
        assert codes[0] == 404, "первые попытки отвечают как обычно"
        # Свой IP при этом не наказан: расписание ARDORIUM ходит редко.
        assert client.get(URL, headers={"X-Api-Token": TOKEN}).status_code == 200


def test_route_not_in_public_catalogue():
    # /openapi.json и /docs открыты анониму. Адрес, который открывается
    # статическим токеном, в этом каталоге не место: обещание «404, чтобы не
    # подтверждать существование» иначе ничего не стоит.
    with _stand(_sub(451, "dl:aaa1111", "asked")) as (client, _):
        schema = client.get("/openapi.json")
        assert schema.status_code == 200, "каталог публичный — это и есть повод"
        assert URL not in schema.json()["paths"]
        assert "channel-tags" not in schema.text


# ─── (г) сторож ПД ───────────────────────────────────────────────────────────

def test_no_personal_data_in_body():
    with _stand(
        _sub(700100200, "dl:aaa1111", "in_channel", confirmed=True, invited=True,
             username="nikolina", first_name="Мария"),
        _sub(700100201, "dl:bbb2222", "asked", username="petrov", first_name="Пётр"),
    ) as (client, _):
        body = client.get(URL, headers={"X-Api-Token": TOKEN}).text
        for key in ("telegram_id", "username", "first_name", "invite_link",
                    "pending_request"):
            assert key not in body, f"ключ {key} уехал наружу"
        for value in ("700100200", "700100201", "nikolina", "petrov",
                      "Мария", "Пётр", "secret-invite"):
            assert value not in body, f"значение {value!r} уехало наружу"
        # И то же самое на уровне функции: лишних ключей нет.
        with _stand(_sub(701, "dl:aaa1111", "asked")) as (_, session):
            row = channel_gate.tag_counts(session)[0]
            assert set(row) == {"tag", "total", "first_seen", "last_seen",
                                *channel_gate.TAG_STATUSES}
        # Вложенный стенд не должен уносить с собой подмену сессии внешнего:
        # без этого запрос уходил в боевой SessionLocal и лез по сети в Postgres.
        assert client.get(URL, headers={"X-Api-Token": TOKEN}).status_code == 200


# ─── (д) метка длиннее колонки ───────────────────────────────────────────────

def test_long_tag_does_not_break():
    # Бот режет метку сам (clean_tag → 10 знаков + префикс), но отчёт не должен
    # зависеть от чужой аккуратности: для него метка — просто строка.
    long_tag = "dl:" + "z" * 20
    with _stand(_sub(501, long_tag, "asked")) as (client, _):
        rows = _rows(client)
        assert long_tag in rows and rows[long_tag]["asked"] == 1


def test_prefix_param_and_like_wildcards():
    with _stand(
        _sub(601, "dl:aaa1111", "asked"),
        _sub(602, "jr:bbb2222", "confirmed"),
    ) as (client, _):
        assert set(_rows(client, prefix="jr:")) == {"jr:bbb2222"}
        # `%` и `_` — знаки LIKE. Экранированные, они ничего не находят, и
        # чужие источники в отчёт рассылки не подмешиваются.
        assert _rows(client, prefix="%") == {}
        assert _rows(client, prefix="dl_") == {}
        assert set(_rows(client, prefix="")) == {"dl:aaa1111"}, "пустой prefix = dl:"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} тестов пройдено.")
