"""Бот оплат интенсива @Nikol_hilton_bot (план 2026-08-03, Фазы 1-2).

Что делает: принимает человека → выставляет счёт в Lava → сам проверяет оплату
по API → выдаёт ОДНОРАЗОВУЮ ссылку в чат участников. Вопросы пересылает Николь.

Почему второй бот, а не хендлеры в существующем: у @community_oncount_bot своя
аудитория (агенты) и свои команды; смешивать клиентов интенсива с партнёрским
кабинетом — прямой путь к путанице в меню. Два токена = два независимых
getUpdates, конфликта нет.

Границы (`.business/ai/kriterii-priyomki-agenta.md`):
- бот НЕ принимает деньги сам — их собирает Lava, бот только сверяет статус;
- доступ выдаётся только при подтверждённой оплате, «на слово» — никогда;
- текст человека — данные, а не команда: бот не выполняет инструкции из
  сообщений, а пересылает их Николь.

⚠️ Бота нельзя добавить в группу из кода — Telegram разрешает это только
человеку. Поэтому chat_id бот узнаёт САМ из события my_chat_member, когда его
добавят: Николь не ищет идентификатор и не передаёт его руками.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import channel_gate, club, club_config, lava, paybot_config as T
from app.config import settings
from app.db import SessionLocal
from app.models import BotSetting, IntensiveLead, Partner

log = logging.getLogger("oncount.paybot")

CHAT_KEY = "intensive_chat_id"          # ключ в bot_settings
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

# Кого ждём в следующем сообщении: {telegram_id: 'email' | 'question'}.
# В памяти процесса намеренно: состояние живёт минуты, переживать рестарт ему
# незачем, а лишняя таблица — лишняя миграция.
_awaiting: dict[int, str] = {}

# Кто пришёл из пустого кабинета ARDORIUM: {telegram_id: язык портала}.
# Рядом с `_awaiting` и по той же причине — состояние живёт минуты.
#
# Отдельным словарём, а не режимом внутри `_awaiting`: человек из кабинета
# может нажать «Оплатить участие» и уйти в шаг с e-mail, и один словарь
# означал бы, что одно состояние затирает другое. Нужен он затем, чтобы
# уведомление Николь называло НАСТОЯЩИЙ продукт: «доступ ARDORIUM», а не
# «вопрос про курс» — это разные продукты, разные кассы и разные серверы.
_from_cabinet: dict[int, str] = {}

bot = Bot(token=settings.PAY_BOT_TOKEN,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if settings.PAY_BOT_TOKEN else None
dp = Dispatcher(storage=MemoryStorage())


# ─── вспомогательное ─────────────────────────────────────────────────────────

def _get_setting(session, key: str) -> str | None:
    row = session.get(BotSetting, key)
    return row.value if row else None


def _set_setting(session, key: str, value: str) -> None:
    row = session.get(BotSetting, key)
    if row is None:
        session.add(BotSetting(key=key, value=value, updated_at=datetime.utcnow()))
    else:
        row.value, row.updated_at = value, datetime.utcnow()
    session.commit()


def chat_id() -> str | None:
    """Чат участников: переменная окружения важнее (ручное переопределение),
    иначе — то, что бот узнал сам при добавлении в группу.

    Недоступная БД не должна ронять бота: вызывается в том числе при старте и в
    фоновом цикле, где исключение убило бы весь поток приёма оплат. Нет ответа —
    считаем, что чат не подключён, и человек получает вежливое «пришлю позже».
    """
    if settings.INTENSIVE_CHAT_ID:
        return settings.INTENSIVE_CHAT_ID
    try:
        with SessionLocal() as s:
            return _get_setting(s, CHAT_KEY)
    except Exception as exc:  # noqa: BLE001 — БД недоступна, не валим бота
        log.warning("paybot chat_id: БД недоступна (%s)", type(exc).__name__)
        return None


def _lead(session, msg_from) -> IntensiveLead:
    lead = (session.query(IntensiveLead)
            .filter_by(telegram_id=msg_from.id).first())
    if lead is None:
        lead = IntensiveLead(telegram_id=msg_from.id, username=msg_from.username,
                             first_name=msg_from.first_name, status="new")
        session.add(lead)
        session.commit()
    return lead


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.BTN_PAY, callback_data="pay:start")],
        [InlineKeyboardButton(text=T.BTN_ASK, callback_data="pay:ask")],
        [InlineKeyboardButton(text=club_config.BTN_CLUB_JOIN,
                              callback_data="club:join")],
    ])


def _fmt_amount(currency: str, amount: float) -> str:
    """Сумма так, как её прочитает человек: без хвоста .0 и с пробелами."""
    whole = f"{amount:,.2f}".replace(",", " ").replace(".00", "")
    return {"RUB": f"{whole} ₽", "EUR": f"€{whole}", "USD": f"${whole}"}.get(
        currency, f"{whole} {currency}")


async def _notify_admin(text: str) -> None:
    """Сообщение владельцу. Best-effort: сбой уведомления не ломает поток оплаты."""
    if not (bot and settings.ADMIN_TG_ID):
        return
    try:
        await bot.send_message(settings.ADMIN_TG_ID, text, disable_web_page_preview=True)
    except Exception as exc:
        log.warning("paybot admin notify failed: %s", type(exc).__name__)


# ─── старт и меню ────────────────────────────────────────────────────────────

def parse_payload(payload: str) -> tuple[bool, str | None]:
    """Разбирает payload диплинка → (сразу к оплате?, ref-слаг агента).

    Понимаем три формы, потому что ссылка собирается в разных местах:
    `pay` (кнопка «купить» на лендинге), `ref_<slug>` (ссылка агента) и
    `pay_ref_<slug>` (кнопка агента, ведущая сразу к оплате). Слаг режем до 16
    символов — ровно как колонка `intensive_leads.ref_slug`, чтобы длинный
    мусор из ссылки не ронял вставку в базу.
    """
    payload = (payload or "").strip()
    to_pay = payload == "pay" or payload.startswith("pay_")
    rest = payload[len("pay_"):] if payload.startswith("pay_") else payload
    ref = rest[len("ref_"):][:16] if rest.startswith("ref_") else None
    return to_pay, (ref or None)


def is_cabinet_empty(payload: str) -> bool:
    """Пришёл ли человек из пустого кабинета ARDORIUM.

    Метку ставит портал: `w_<язык>_cabinet-empty` (`content_cta.bot_href`).
    Голое `cabinet-empty` понимаем тоже — ссылку могут собрать руками.

    Отдельной функцией, как `parse_payload`: условие решает, в какой поток
    попадёт человек, и проверять его надо прямо, а не через весь обработчик.
    """
    payload = (payload or "").strip()
    return payload == "cabinet-empty" or payload.endswith("_cabinet-empty")


def cabinet_locale(payload: str) -> str:
    """Язык портала из метки `w_<язык>_cabinet-empty`. Не распознан — пусто.

    Язык нужен Николь в уведомлении: у ARDORIUM три языковых контура, и с
    человеком отвечают на его языке. Портал кладёт язык в метку сам
    (`content_cta._start_payload`), терять его на последнем шаге незачем.
    """
    payload = (payload or "").strip()
    if not payload.startswith("w_"):
        return ""
    lang = payload[len("w_"):].split("_", 1)[0].lower()
    return lang if len(lang) == 2 and lang.isalpha() else ""


async def _products_kb() -> InlineKeyboardMarkup | None:
    """Тарифы кнопками с ЖИВЫМИ ценами из кассы. None — касса не ответила.

    Цену не кэшируем и не дублируем в конфиге намеренно: 04.08.2026 оффер в
    Lava переименовали и переоценили, не меняя id, — любая наша копия цены в
    такой момент начинает врать человеку, у которого уже открыт кошелёк.
    """
    # Оба тарифа спрашиваем у кассы ОДНОВРЕМЕННО: по очереди это до двух её
    # таймаутов подряд (25 с каждый), а человек в это время смотрит на пустой
    # чат сразу после нажатия кнопки на сайте.
    items = list(lava.PRODUCTS.items())
    answers = await asyncio.gather(
        *(asyncio.to_thread(lava.offer_prices, offer_id) for _, (offer_id, _l) in items),
        return_exceptions=True,
    )
    rows = []
    for (code, (_offer_id, label)), prices in zip(items, answers):
        if isinstance(prices, BaseException) or not prices:
            log.warning("paybot: касса не дала цену для %s", code)
            continue
        usd = prices.get("USD")
        price = f" — {_fmt_amount('USD', usd)}" if usd else ""
        rows.append([InlineKeyboardButton(text=f"{label}{price}",
                                          callback_data=f"pay:prod:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@dp.message(CommandStart(deep_link=True))
async def start_deep(msg: Message, command: CommandObject) -> None:
    """/start pay — пришёл с кнопки «купить» на лендинге, сразу тарифы.
    /start ref_<slug> — приход по ссылке агента, атрибуция как на /pay.
    /start channel — вход в закрытый канал Николь (план 2026-08-03).
    /start w_<язык>_cabinet-empty — пустой кабинет ARDORIUM (24.08.2026)."""
    payload = (command.args or "").strip()
    if is_cabinet_empty(payload):
        # Кабинет ARDORIUM, а не вопрос про курс: человек уже заплатил и не
        # видит купленного. Ответ на это сообщение приходит сюда же и падает
        # Николь обычным путём вопроса (`on_text` → `_notify_admin`) — второго
        # канала уведомлений не заводим.
        #
        # ⚠️ Ожидания сбрасываем ОБЯЗАТЕЛЬНО, в отличие от веток `channel` и
        # `club`: у тех свои состояния, которые перехватывают следующий текст,
        # а здесь человек пишет свободным текстом прямо в `on_text`. Без сброса
        # тот, кто когда-то бросил оплату на шаге «напишите e-mail», в ответ на
        # рассказ о пропавшем доступе услышал бы «Это не похоже на e-mail» —
        # ровно та грабля, что описана ниже для обычного захода.
        _awaiting.pop(msg.from_user.id, None)
        club.forget(msg.from_user.id)
        _from_cabinet[msg.from_user.id] = cabinet_locale(payload)
        # След в логе: сколько людей пришло сюда — это мера аварии НА ПОРТАЛЕ.
        # Молча ушедшие (посмотрел и закрыл чат) иначе невидимы вовсе, и размер
        # происшествия оценить нечем. В строку кладём только id, не ПД.
        log.info("paybot: приход из пустого кабинета ARDORIUM, payload=%s, id%s",
                 payload, msg.from_user.id)
        # Без клавиатуры намеренно, и это не повторение бага 04.08.2026: там
        # человек хотел заплатить и не мог. Здесь он УЖЕ заплатил, и кнопка
        # «Оплатить участие» предлагала бы ему купить второй раз, да ещё чужой
        # продукт — курс ONCOUNT вместо программ ARDORIUM. Следующее действие
        # названо словами в самом тексте: написать, что случилось.
        await msg.answer(T.CABINET_ACCESS_GREETING)
        return
    if payload == "channel" or payload.startswith("channel-"):
        # Другая аудитория и другой поток: человек пришёл за каналом, оффер
        # интенсива ему сейчас не нужен. Вопрос про 18+ задаёт привратник.
        # Хвост после «channel-» — метка рассылки (`channel-kommo11`): по ней
        # видно, из какой воронки Kommo пришёл человек. Сравнение было ТОЧНЫМ,
        # и любая метка молча роняла человека в оффер интенсива.
        tag = channel_gate.clean_tag(payload[len("channel-"):])
        await channel_gate.ask_age(msg.bot, msg.chat.id, msg.from_user,
                                   f"dl:{tag}" if tag else "deeplink")
        return
    if payload == "club":
        # Третий поток: платный клуб. Оффер интенсива здесь тоже не нужен —
        # человек пришёл по клубной ссылке.
        await club.show_intro(msg.bot, msg.chat.id, msg.from_user)
        return
    to_pay, ref = parse_payload(payload)
    # Новый заход = чистый лист. Иначе человек, который вчера бросил оплату на
    # шаге «напишите e-mail», сегодня приходит с сайта, пишет вопрос — и слышит
    # «это не похоже на e-mail». Клубное ожидание сбрасываем по той же причине.
    _awaiting.pop(msg.from_user.id, None)
    club.forget(msg.from_user.id)
    with SessionLocal() as s:
        lead = _lead(s, msg.from_user)
        if ref and not lead.ref_slug:
            lead.ref_slug = ref
            partner = s.query(Partner).filter_by(ref_slug=ref).first()
            lead.partner_id = partner.id if partner else None
            s.commit()
    if not to_pay:
        await msg.answer(T.GREETING, reply_markup=_menu())
        return
    # Пришёл с кнопки «купить»: здороваемся сразу, не дожидаясь кассы (запрос
    # цен — это сеть), и следом показываем тарифы. Если касса молчит — человек
    # всё равно остаётся с кнопками, а не в тупике.
    await msg.answer(T.GREETING)
    kb = await _products_kb()
    if kb is None:
        await msg.answer(T.PRICES_UNAVAILABLE, reply_markup=_menu())
        return
    await msg.answer(T.PRODUCT_TITLE, reply_markup=kb)


@dp.message(CommandStart())
async def start(msg: Message) -> None:
    _awaiting.pop(msg.from_user.id, None)   # см. комментарий в start_deep
    with SessionLocal() as s:
        _lead(s, msg.from_user)
    await msg.answer(T.GREETING, reply_markup=_menu())


@dp.callback_query(F.data == "pay:start")
async def cb_pay(call) -> None:
    """Сначала ЧТО покупаем: первый день или весь курс. Цены — живые из кассы."""
    kb = await _products_kb()
    if kb is None:
        await call.message.answer(T.PRICES_UNAVAILABLE, reply_markup=_menu())
        await call.answer()
        return
    await call.message.answer(T.PRODUCT_TITLE, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("pay:prod:"))
async def cb_product(call) -> None:
    """Выбрали продукт → показываем валюты с ценами ИМЕННО этого оффера."""
    code = call.data.rsplit(":", 1)[-1]
    if code not in lava.PRODUCTS:
        await call.answer()
        return
    offer_id, label = lava.PRODUCTS[code]
    with SessionLocal() as s:
        lead = _lead(s, call.from_user)
        lead.product_code = code
        s.commit()
    prices = await asyncio.to_thread(lava.offer_prices, offer_id)
    if not prices:
        await call.message.answer(T.PRICES_UNAVAILABLE, reply_markup=_menu())
        await call.answer()
        return
    rows = [[InlineKeyboardButton(
        text=f"{lava.CURRENCY_LABELS.get(c, c)} — {_fmt_amount(c, prices[c])}",
        callback_data=f"pay:cur:{c}")]
        for c in lava.CURRENCIES if c in prices]
    await call.message.answer(label + "\n\n" + T.CURRENCY_TITLE,
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("pay:cur:"))
async def cb_currency(call) -> None:
    currency = call.data.rsplit(":", 1)[-1]
    if currency not in lava.CURRENCIES:
        await call.answer()
        return
    with SessionLocal() as s:
        lead = _lead(s, call.from_user)
        lead.lava_currency = currency
        s.commit()
    _awaiting[call.from_user.id] = "email"
    await call.message.answer(T.EMAIL_ASK)
    await call.answer()


@dp.callback_query(F.data == "pay:ask")
async def cb_ask(call) -> None:
    _awaiting[call.from_user.id] = "question"
    await call.message.answer(T.ASK_INTRO)
    await call.answer()


@dp.callback_query(F.data == "pay:check")
async def cb_check(call) -> None:
    """Ручная проверка оплаты по кнопке (фоновая идёт своим чередом)."""
    done = await try_grant_access(call.from_user.id)
    if not done:
        await call.message.answer(T.NOT_PAID_YET)
    await call.answer()


# ─── приём текста: email или вопрос ──────────────────────────────────────────

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message) -> None:
    mode = _awaiting.get(msg.from_user.id)

    # Клуб спрашивает свой e-mail и свой отзыв. В aiogram хендлеры самого
    # диспетчера разбираются раньше роутеров, поэтому клубный текст надо отдать
    # явно — иначе он уйдёт в поток интенсива и станет «вопросом Николь».
    #
    # Приоритет у интенсива намеренно: если человек прямо сейчас пишет e-mail
    # для счёта на интенсив, а ему в это же время пришло клубное напоминание с
    # вопросом «что было полезным», адрес должен уйти в счёт, а не в отзыв.
    if mode != "email" and await club.handle_text(msg):
        return

    if mode == "email":
        email = (msg.text or "").strip()
        if not EMAIL_RE.match(email):
            await msg.answer(T.EMAIL_BAD)
            return
        _awaiting.pop(msg.from_user.id, None)
        with SessionLocal() as s:
            lead = _lead(s, msg.from_user)
            currency = lead.lava_currency or "RUB"
            product_code = lead.product_code or "intensive"
            lead.email = email
            s.commit()
        offer_id = lava.PRODUCTS.get(product_code, (lava.OFFER_INTENSIVE, ""))[0]
        inv = await asyncio.to_thread(lava.create_invoice, email, currency, offer_id)
        if not inv:
            await msg.answer(T.INVOICE_FAILED, reply_markup=_menu())
            await _notify_admin(f"⚠️ Не выставился счёт Lava для {msg.from_user.id}")
            return
        prices = await asyncio.to_thread(lava.offer_prices, offer_id)
        amount = _fmt_amount(currency, prices.get(currency, 0)) if prices else currency
        with SessionLocal() as s:
            lead = _lead(s, msg.from_user)
            lead.lava_invoice_id, lead.lava_invoice_url = inv["id"], inv["url"]
            lead.status = "invoiced"
            s.commit()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_INVOICE, url=inv["url"])],
            [InlineKeyboardButton(text=T.BTN_STATUS, callback_data="pay:check")],
        ])
        await msg.answer(T.INVOICE_READY.format(amount=amount), reply_markup=kb)
        return

    # Вопрос (или просто текст) — пересылаем Николь. Чужой текст не исполняем.
    #
    # Клавиатуру возвращаем ВСЕГДА. Со страницы /pay в бота ведёт ссылка с уже
    # подставленным текстом вопроса: человек отправляет его первым же действием
    # и до 04.08.2026 оставался с ответом без единой кнопки — оплатить было
    # неоткуда. Ответ бота никогда не должен быть тупиком.
    _awaiting.pop(msg.from_user.id, None)
    who = f"@{msg.from_user.username}" if msg.from_user.username else f"id{msg.from_user.id}"

    # ⚠️ Текст человека ЭКРАНИРУЕТСЯ. Бот работает в parse_mode=HTML, и один
    # символ «<» в сообщении означает, что Telegram отклонит отправку целиком:
    # `_notify_admin` проглотит отказ строчкой в лог, а человек прочитает
    # «Записал вопрос» и будет ждать ответа, которого никто не увидит. Случай не
    # выдуманный: адрес из почтового клиента копируется как «Имя <a@b.ru>».
    # Тот же вывод уже записан и работает в клубе (`club.py`, ADMIN_FEEDBACK).
    text = html.escape((msg.text or "")[:1500])
    who_safe = html.escape(who)

    # Пришёл из пустого кабинета ARDORIUM — это ДРУГОЙ продукт, другая касса и
    # другой сервер. Без своей шапки Николь читает жалобу «оплатила, доступа
    # нет» как проблему с оплатой курса и идёт разбираться не туда.
    locale = _from_cabinet.pop(msg.from_user.id, None)
    if locale is not None:
        await _notify_admin(T.ADMIN_CABINET.format(
            who=who_safe, lang=f" (язык {locale})" if locale else "", text=text))
        # Ответ без кнопок оплаты — разбор в ветке `start_deep`. Лид в
        # `intensive_leads` не заводим: это воронка ИНТЕНСИВА, и кабинетные
        # приходы испортили бы её счёт.
        await msg.answer(T.CABINET_ACCESS_RECEIVED)
        return

    with SessionLocal() as s:
        _lead(s, msg.from_user)
    await _notify_admin(f"💬 Вопрос в боте оплат от {who_safe}:\n\n{text}")
    await msg.answer(T.ASK_RECEIVED, reply_markup=_menu())


# ─── бота добавили в группу: узнаём chat_id сам ──────────────────────────────

@dp.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_added_to_chat(ev: ChatMemberUpdated) -> None:
    """Ключевой обработчик: бота нельзя завести в чат из кода, но когда человек
    его добавит, Telegram присылает это событие. Ловим chat_id, проверяем права
    и сразу говорим Николь, чего не хватает."""
    chat = ev.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = ev.new_chat_member.status
    if status in ("left", "kicked"):
        log.info("paybot удалён из чата %s", chat.id)
        return

    can_invite = bool(getattr(ev.new_chat_member, "can_invite_users", False))

    # Чат перезаписываем ОСМОТРИТЕЛЬНО. 03.08.2026 бота добавили в два чата
    # подряд: сначала в служебный (без прав), следом в настоящий (админом).
    # Слепая перезапись «последним победившим» означала бы, что случайное
    # добавление в посторонний чат молча уводит выдачу доступа не туда.
    # Правило: рабочим считается чат, где у бота ЕСТЬ право на приглашения;
    # чат без прав не вытесняет уже настроенный.
    with SessionLocal() as s:
        current = _get_setting(s, CHAT_KEY)
        if can_invite or not current:
            _set_setting(s, CHAT_KEY, str(chat.id))
            replaced = bool(current and current != str(chat.id))
        else:
            replaced = False
            log.info("paybot: чат %s без прав, оставляем настроенный %s", chat.id, current)
            await _notify_admin(
                f"ℹ️ Бота добавили в чат «{html.escape(str(chat.title or chat.id))}», "
                f"но прав на приглашения там нет. Рабочим остаётся чат {current}.")
            return

    log.info("paybot добавлен в чат %s (%s), can_invite=%s", chat.id, status, can_invite)
    note = T.CHAT_RIGHTS_OK if can_invite else T.CHAT_RIGHTS_MISSING
    if replaced:
        note += f"\n\n(Прежний чат {current} заменён на этот.)"
    # Название чата задаёт человек — экранируем по той же причине, что и текст
    # вопроса: «<» в названии означает недоставленное уведомление.
    await _notify_admin(T.CHAT_LINKED_ADMIN.format(
        title=html.escape(str(chat.title or "без названия")), chat_id=chat.id, rights=note))


# ─── выдача доступа ──────────────────────────────────────────────────────────

async def try_grant_access(telegram_id: int) -> bool:
    """Оплатил? → одноразовый инвайт в чат. Идемпотентно: повторный вызов не
    выдаёт вторую ссылку и не шлёт второе уведомление."""
    with SessionLocal() as s:
        lead = s.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
        if lead is None or not lead.lava_invoice_id:
            return False
        if lead.status == "in_chat" and lead.invite_link:
            return True
        invoice_id, currency = lead.lava_invoice_id, lead.lava_currency or "RUB"
        already_paid = lead.status in ("paid", "in_chat")

    if not already_paid:
        paid = await asyncio.to_thread(lava.invoice_paid, invoice_id)
        if not paid:
            return False
        with SessionLocal() as s:
            lead = s.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
            if lead.status not in ("paid", "in_chat"):
                lead.status, lead.paid_at = "paid", datetime.utcnow()
                s.commit()
        with SessionLocal() as s2:
            _l = s2.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
            offer_id = lava.PRODUCTS.get(_l.product_code or "intensive",
                                         (lava.OFFER_INTENSIVE, ""))[0]
        prices = await asyncio.to_thread(lava.offer_prices, offer_id)
        who = f"id{telegram_id}"
        with SessionLocal() as s:
            lead = s.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
            who = f"@{lead.username}" if lead.username else who
            agent = f"Агент: {lead.ref_slug}" if lead.ref_slug else "Агент: —"
        await _notify_admin(T.ADMIN_PAID.format(
            who=who, amount=_fmt_amount(currency, prices.get(currency, 0)),
            invoice=invoice_id, agent=agent))

    cid = chat_id()
    if not cid:
        # Оплата есть, чата ещё нет — человек не виноват, пусть ждёт спокойно.
        await bot.send_message(telegram_id, T.PAID_NO_CHAT)
        await _notify_admin("⚠️ Оплата есть, но чат не подключён: добавьте бота "
                            "в чат участников админом с правом приглашать.")
        return True

    try:
        link = await bot.create_chat_invite_link(
            chat_id=cid, member_limit=1, name=f"pay-{telegram_id}")
        with SessionLocal() as s:
            lead = s.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
            lead.invite_link, lead.invited_at = link.invite_link, datetime.utcnow()
            lead.status = "in_chat"
            s.commit()
        await bot.send_message(telegram_id, T.PAID_OK.format(link=link.invite_link))
        await _send_club_promo(telegram_id)
        return True
    except Exception as exc:
        log.error("paybot invite failed: %s", type(exc).__name__)
        await bot.send_message(telegram_id, T.PAID_NO_CHAT)
        await _notify_admin(f"⚠️ Не удалось создать инвайт: {type(exc).__name__}. "
                            "Проверьте, что бот админ и может приглашать.")
        return True


async def _send_club_promo(telegram_id: int) -> None:
    """Промокод на бесплатный первый месяц клуба — покупателям ПОЛНОГО интенсива.

    Решение Николь 04.08.2026. Тем, кто взял только первый день за $20, промокод
    не полагается. Отметка `club_promo_sent_at` защищает от повторной выдачи:
    проверка оплат крутится каждую минуту, и без неё человек получал бы промокод
    снова и снова.
    """
    with SessionLocal() as s:
        lead = s.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
        if lead is None or lead.club_promo_sent_at:
            return
        if (lead.product_code or "intensive") != "intensive":
            return
    try:
        await bot.send_message(telegram_id, T.CLUB_PROMO_TEXT,
                               disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001 — доступ уже выдан, промокод вторичен
        log.warning("paybot club promo failed: %s", type(exc).__name__)
        return
    with SessionLocal() as s:
        lead = s.query(IntensiveLead).filter_by(telegram_id=telegram_id).first()
        if lead is not None:
            lead.club_promo_sent_at = datetime.utcnow()
            s.commit()
    log.info("paybot: промокод клуба выдан %s", telegram_id)


async def poll_payments_once() -> int:
    """Проверить всех, кто получил счёт, но ещё не в чате. Возвращает, скольким
    выдали доступ. Вызывается по расписанию — человек не должен жать кнопку."""
    if not (bot and lava.is_configured()):
        return 0
    with SessionLocal() as s:
        ids = [r.telegram_id for r in s.query(IntensiveLead)
               .filter(IntensiveLead.status.in_(("invoiced", "paid"))).all()]
    granted = 0
    for tid in ids:
        try:
            if await try_grant_access(tid):
                granted += 1
        except Exception as exc:
            log.warning("poll_payments %s: %s", tid, type(exc).__name__)
    return granted


async def _payment_loop() -> None:
    """Фоновая проверка оплат раз в минуту: человек оплатил — доступ пришёл сам."""
    while True:
        try:
            await asyncio.sleep(60)
            n = await poll_payments_once()
            if n:
                log.info("paybot: выдан доступ %s людям", n)
            # Клубные счета проверяются тем же тактом: у них своя таблица и свой
            # канал, но ждать доступ человек не должен дольше минуты ни там, ни там.
            if await club.poll_payments_once(bot):
                log.info("club: доступ выдан по оплате подписки")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("paybot payment loop: %s", type(exc).__name__)


@dp.message(Command("id"))
async def cmd_id(msg: Message) -> None:
    """Служебная команда: показать chat_id текущего чата (для отладки связки)."""
    await msg.answer(f"chat_id: <code>{msg.chat.id}</code>")


@dp.message(Command("channelstatus"))
async def cmd_channel_status(msg: Message) -> None:
    """Проверка «канал частный и бот там админ» — одной командой, по данным
    Telegram. Только для Николь: остальным знать id канала незачем."""
    if not settings.ADMIN_TG_ID or msg.from_user.id != settings.ADMIN_TG_ID:
        return
    await msg.answer(await channel_gate.channel_status(msg.bot))


@dp.message(Command("club"))
async def cmd_club(msg: Message) -> None:
    """Вход в платный клуб: что это и кнопка вступления."""
    await club.show_intro(msg.bot, msg.chat.id, msg.from_user)


@dp.message(Command("channel"))
async def cmd_channel(msg: Message) -> None:
    """Вход в закрытый канал: тот же вопрос про 18+, что и по ссылке.
    Нужен, чтобы человеку было куда вернуться, когда персональная ссылка истечёт."""
    await channel_gate.ask_age(msg.bot, msg.chat.id, msg.from_user, "deeplink")


async def main() -> None:
    if bot is None:
        log.info("PAY_BOT_TOKEN пуст → бот оплат не поднимается")
        return
    # Привратник канала — отдельным роутером: свои события (заявки, канал в
    # my_chat_member), свои тексты. Хендлеры самого dp разбираются раньше, так
    # что поток интенсива остаётся нетронутым.
    dp.include_router(channel_gate.router)
    # Клуб — третий поток в том же боте. Роутер отдельный: свои callback'и
    # (club:*), своя таблица, свой канал. Пересечься с интенсивом он может
    # только на тексте — там передача явная, см. on_text.
    dp.include_router(club.router)
    problems = await asyncio.to_thread(lava.check_offers)
    for p in problems:
        log.error("ВНИМАНИЕ, касса: %s", p)
    if problems:
        await _notify_admin(
            "⚠️ Касса Lava разошлась с ботом:\n\n"
            + "\n".join(f"• {p}" for p in problems)
            + "\n\nПока не поправить, бот может продавать не то.")
    me = await bot.get_me()
    log.info("Paybot polling start, bot=@%s, lava=%s, chat=%s, channel=%s",
             me.username, lava.is_configured(), chat_id() or "не подключён",
             channel_gate.channel_id() or "не подключён")
    asyncio.create_task(_payment_loop())
    asyncio.create_task(club.loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
