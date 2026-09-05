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
import logging
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
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import auth  # noqa: E402
from app import channel_gate  # noqa: E402
from app import main as web  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, ChannelSubscriber, Partner  # noqa: E402

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


@contextmanager
def _logs():
    """Журнал приложения за время блока: наружная дверь обязана оставлять след,
    и стеречь это можно только заглянув в логи."""
    written = []

    class Catch(logging.Handler):
        def emit(self, record):
            written.append(f"{record.levelname} {record.getMessage()}")

    handler, root = Catch(), logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield written
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


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
        assert a["first_seen"] == "2026-09-02T12:00:00"
        assert a["last_seen"] == "2026-09-03T12:10:00"

        b = rows["dl:bbb2222"]
        assert b["asked"] == 1 and sum(b[s] for s in channel_gate.TAG_STATUSES) == 1
        # Ни одной даты не потеряли: без invited/confirmed берётся created_at.
        assert b["first_seen"] == b["last_seen"] == "2026-09-04T12:00:00"


def test_row_keys_exactly_as_agreed():
    # Форма строки задана поимённо и по порядку: девять ключей, ничего сверх.
    # Читающая сторона сверяет карточку по именам, и лишний ключ здесь — это
    # разговор с ARDORIUM, а не мелочь.
    with _stand(_sub(801, "dl:aaa1111", "asked", confirmed=True)) as (client, session):
        row = _rows(client)["dl:aaa1111"]
        assert list(row) == ["tag", "asked", "confirmed", "invited", "in_channel",
                             "left", "declined", "first_seen", "last_seen"]
        assert list(channel_gate.tag_counts(session)[0]) == list(row), \
            "функция и маршрут обязаны отдавать одну и ту же форму"


def test_six_counters_cover_everyone():
    # Отдельного `total` в ответе нет, и держится это на том, что статусов ровно
    # шесть: `status` — один столбец, седьмого значения привратник не пишет.
    # Сторож проверяет саму опору: сумма шести = число строк с меткой.
    with _stand(
        _sub(811, "dl:aaa1111", "in_channel"), _sub(812, "dl:aaa1111", "left"),
        _sub(813, "dl:aaa1111", "declined"), _sub(814, "dl:bbb2222", "invited"),
    ) as (client, session):
        rows = _rows(client)
        for tag, row in rows.items():
            в_базе = (session.query(ChannelSubscriber)
                      .filter(ChannelSubscriber.source == tag).count())
            assert sum(row[st] for st in channel_gate.TAG_STATUSES) == в_базе
        assert set(channel_gate.TAG_STATUSES) == {
            "asked", "confirmed", "invited", "in_channel", "left", "declined"}


def test_rows_sorted_by_tag():
    # Порядок задан по метке, а не по дате: две выгрузки подряд обязаны давать
    # одинаковый порядок, иначе ARDORIUM прочитает перестановку как изменение.
    with _stand(
        _sub(201, "dl:staraya", "asked", days_ago=30),
        _sub(202, "dl:novaya", "asked", days_ago=1),
        _sub(203, "dl:aaa0000", "asked", days_ago=10),
    ) as (client, _):
        было = [r["tag"] for r in client.get(URL, headers={"X-Api-Token": TOKEN}).json()["rows"]]
        стало = [r["tag"] for r in client.get(URL, headers={"X-Api-Token": TOKEN}).json()["rows"]]
        assert было == ["dl:aaa0000", "dl:novaya", "dl:staraya"], "порядок не по метке"
        assert было == стало


def test_no_subscribers_empty_rows():
    with _stand() as (client, _):
        body = client.get(URL, headers={"X-Api-Token": TOKEN}).json()
        assert body["rows"] == []
        # Формат `generated_at` — как у соседнего /healthz: голый isoformat
        # наивного UTC, без «Z». Разбирается обратно без единой поправки.
        assert not body["generated_at"].endswith("Z")
        assert datetime.fromisoformat(body["generated_at"]).year == datetime.utcnow().year


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


def _owner_cookie(session, client, telegram_id=None):
    """Настоящая кука владельца: партнёр в своей же базе + подписанный JWT.
    Без подмены require_admin — она вызывается руками внутри тела маршрута,
    и dependency_overrides до неё не достаёт."""
    partner = Partner(telegram_id=telegram_id if telegram_id is not None
                      else settings.ADMIN_TG_ID,
                      ref_slug=f"ref{telegram_id or 'adm'}"[:16], status="active")
    session.add(partner)
    session.commit()
    client.cookies.set(auth.COOKIE_NAME, auth.issue_jwt(partner.id))


def test_owner_cookie_opens_the_door():
    # Вторая дверь целиком: кука Николь, а не подмена проверки.
    with _stand(_sub(421, "dl:aaa1111", "asked")) as (client, session):
        _owner_cookie(session, client)
        assert client.get(URL).status_code == 200
        # Неверный токен при живой куке — не отказ, а переход к следующей двери:
        # иначе Николь получала бы 404, случайно прислав старый заголовок.
        assert client.get(URL, headers={"X-Api-Token": "starye-klyuchi"}).status_code == 200


def test_stranger_cookie_still_sees_404():
    # Чужой партнёр с валидной кукой — посторонний: раздел один и только Николь.
    with _stand(_sub(431, "dl:aaa1111", "asked")) as (client, session):
        _owner_cookie(session, client, telegram_id=settings.ADMIN_TG_ID + 1)
        assert client.get(URL).status_code == 404


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
        # В ответе эхом лежит присланный `prefix`; угадывать тип браузеру нечего.
        assert r.headers.get("x-content-type-options") == "nosniff"


def test_token_brute_force_is_capped():
    # Подбор токена — единственный способ войти без cookie. Адрес обязан жить
    # под тем же потолком, что вход и приглашения (30 запросов с IP за минуту).
    with _stand(_sub(441, "dl:aaa1111", "asked")) as (client, _):
        chuzhoy = {"X-Forwarded-For": "203.0.113.7"}   # свой бакет, соседей не трогаем
        codes = [client.get(URL, headers={"X-Api-Token": f"podbor-{i}", **chuzhoy}).status_code
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


def test_closed_door_leaves_a_trace():
    # Если ключ разойдётся, наружу это выглядит как тишина: 404 и всё. Один
    # предупреждающий след отличает «ARDORIUM не ходит» от «ходит не с тем
    # ключом» — без него разбирать нечем. Сам ключ в журнал попасть не должен.
    with _stand(_sub(461, "dl:aaa1111", "asked")) as (client, _):
        with _logs() as written:
            client.get(URL, headers={"X-Api-Token": "sovsem-ne-tot-klyuch"})
        след = [w for w in written if "channel-tags" in w]
        assert след and след[0].startswith("WARNING"), "закрытая дверь молчит"
        assert "sovsem-ne-tot-klyuch" not in "\n".join(written), "ключ уехал в журнал"
        # Аноним без заголовка предупреждения не заслуживает: это не сбой связки.
        with _logs() as written:
            client.get(URL)
        assert not [w for w in written if "channel-tags" in w and "WARNING" in w]
        # И признак жизни: цифры кто-то забрал.
        with _logs() as written:
            client.get(URL, headers={"X-Api-Token": TOKEN})
        assert [w for w in written if w.startswith("INFO") and "channel-tags" in w]


def test_prefix_cannot_forge_a_log_line():
    # Свою же правку и проверяю: запись в журнал появилась на ходе 6, а вместе с
    # ней — возможность вписать в него чужую строку. Перевод строки в query
    # разрывает запись пополам, и вторая половина читается как отдельная.
    with _stand(_sub(491, "dl:aaa1111", "asked")) as (client, _):
        with _logs() as written:
            client.get(URL, params={"prefix": "dl:\nWARNING channel-tags: всё хорошо"},
                       headers={"X-Api-Token": TOKEN})
        наши = [w for w in written if "channel-tags" in w]
        assert наши, "запись о выдаче пропала"
        assert "\n" not in наши[0], "чужая строка попала в журнал целой строкой"


def test_prefix_case_normalized_by_us_not_by_engine():
    # У SQLite (стенд) LIKE к регистру нечувствителен, у Postgres (прод) —
    # чувствителен: замер на живой базе показал `DL:` → 300 строк на стенде и 0
    # на проде. Поэтому регистр приводим сами, и стеречь надо именно это —
    # результат запроса на SQLite одинаков с правкой и без неё.
    with _stand(_sub(471, "dl:aaa1111", "asked")) as (client, _):
        body = client.get(URL, params={"prefix": "DL:"},
                          headers={"X-Api-Token": TOKEN}).json()
        assert body["prefix"] == "dl:", "префикс не нормализован — прод найдёт пустоту"
        assert len(body["rows"]) == 1


def test_endpoint_only_reads():
    # Задание: «эндпоинт только читает». Обещание жило в докстроке; теперь его
    # стережёт список всех запросов, которые ушли в базу за время ответа.
    with _stand(_sub(481, "dl:aaa1111", "asked", confirmed=True)) as (client, session):
        seen = []

        @event.listens_for(session.get_bind(), "before_cursor_execute")
        def catch(conn, cursor, statement, *a):    # noqa: ANN001
            seen.append(statement.strip().split()[0].upper())

        try:
            assert client.get(URL, headers={"X-Api-Token": TOKEN}).status_code == 200
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", catch)
        assert seen, "стенд не поймал ни одного запроса — стеречь было бы нечего"
        assert set(seen) <= {"SELECT"}, f"наружу ушёл не только SELECT: {set(seen)}"
        assert not (session.new or session.dirty or session.deleted)


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
            assert set(row) == {"tag", "first_seen", "last_seen",
                                *channel_gate.TAG_STATUSES}
        # Вложенный стенд не должен уносить с собой подмену сессии внешнего:
        # без этого запрос уходил в боевой SessionLocal и лез по сети в Postgres.
        assert client.get(URL, headers={"X-Api-Token": TOKEN}).status_code == 200


# ─── (д) метка длиннее колонки ───────────────────────────────────────────────

def test_tag_at_the_column_boundary():
    # Прежний сторож проверял невозможное: метку длиннее 16 знаков бот не делает
    # вовсе, а Postgres такую в String(16) и не вставит. Настоящая граница —
    # `dl:` + 10 знаков от clean_tag, то есть 13: столько бот выдаёт в худшем
    # случае, и столько отчёт обязан переварить.
    хвост = channel_gate.clean_tag("z" * 40)
    assert len(хвост) == 10, "clean_tag режет до 10 — на этом держится расчёт"
    метка = f"dl:{хвост}"
    assert len(метка) == 13, "худший случай от бота: dl: + 10 знаков"
    assert len(метка) <= ChannelSubscriber.source.type.length, "не влезает в столбец"
    with _stand(_sub(501, метка, "asked")) as (client, _):
        rows = _rows(client)
        assert метка in rows and rows[метка]["asked"] == 1


def test_ardorium_code_survives_clean_tag():
    # Код рассылки — 7 знаков из строчного алфавита без i/l/o/0/1. Он обязан
    # доехать до отчёта буква в букву: сопоставление на стороне ARDORIUM идёт
    # по нему. Заглавные и лишние знаки clean_tag НЕ пропускает — это и видно.
    for код in ("k7m2xqp", "b4n9wzr", "abcdefg", "23456789"[:7]):
        assert channel_gate.clean_tag(код) == код, f"код {код} исказился"
    assert channel_gate.clean_tag("K7M2XQP") == "k7m2xqp", "регистр опускается"
    assert channel_gate.clean_tag("ab7-x9k") == "ab7", "по знаку не из алфавита режется"


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
