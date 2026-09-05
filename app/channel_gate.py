"""Привратник закрытого канала Николь: 18+ → доступ (план 2026-08-03).

Что делает: спрашивает возраст, записывает подтверждение в базу и открывает
канал — одобрением заявки на вступление либо персональной одноразовой ссылкой.

Два входа, один привратник:
1. `t.me/<бот>?start=channel` — основной путь, эту ссылку Николь публикует;
2. заявка на вступление в канал — страховка для тех, у кого осталась старая
   ссылка. Telegram даёт боту `user_chat_id` и разрешает написать автору заявки,
   даже если тот никогда не открывал бота (проверено на aiogram 3.4.1).

Живёт отдельным роутером внутри бота оплат (@nikol_hilton_bot): бот один, но
потоки разные — обычный /start остаётся оффером интенсива.

Границы: возраст — самодекларация, документы не спрашиваем. Ценность записи в
том, что у неё есть дата. ПД (`telegram_id`, имя, username) в лог не пишем.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from app import channel_config as T
from app.config import settings
from app.db import SessionLocal
from app.models import BotSetting, ChannelSubscriber

log = logging.getLogger("oncount.channel")

CHANNEL_KEY = "nikol_channel_id"     # ключ в bot_settings
LINK_TTL_HOURS = 24                  # сколько живёт персональная ссылка
IN_CHANNEL_STATUSES = ("member", "administrator", "creator")

router = Router(name="channel_gate")

_TAG_RE = re.compile(r"[A-Za-z0-9]+")


# ─── вспомогательное ─────────────────────────────────────────────────────────

def clean_tag(raw: str | None) -> str:
    """Метка источника: из deep-link (`?start=channel-kommo11`) или из имени
    пригласительной ссылки, которой помечен сегмент рассылки.

    Режем жёстко: `ChannelSubscriber.source` — String(16), и туда ещё уходит
    префикс `dl:` / `jr:`. Метка длиннее 10 символов поле не переживёт, а
    молчаливая обрезка на стороне Postgres — это 502 на ровном месте.
    Кириллица в имени ссылки метки не даёт: имена сегментов только латиницей.
    """
    if not raw:
        return ""
    found = _TAG_RE.search(raw)
    return found.group(0).lower()[:10] if found else ""


def channel_id() -> str | None:
    """Id канала: переменная окружения важнее (ручное переопределение), иначе —
    то, что бот узнал сам, когда его сделали админом.

    Недоступная БД не должна ронять поток: нет ответа — считаем, что канал не
    подключён, и человек получает вежливое «пришлю позже».
    """
    if settings.NIKOL_CHANNEL_ID:
        return settings.NIKOL_CHANNEL_ID
    try:
        with SessionLocal() as s:
            row = s.get(BotSetting, CHANNEL_KEY)
            return row.value if row else None
    except Exception as exc:  # noqa: BLE001 — БД недоступна, не валим бота
        log.warning("channel_id: БД недоступна (%s)", type(exc).__name__)
        return None


def _remember_channel(cid: int, can_invite: bool = True) -> tuple[bool, str | None]:
    """Запомнить канал. Возвращает (записали ли, какой канал вытеснили).

    Перезаписываем ОСМОТРИТЕЛЬНО — тот же урок, что бот оплат выучил 03.08.2026
    на двух чатах подряд: если бота добавят админом в посторонний канал, слепое
    «последний победил» молча уведёт выдачу доступа не туда. Рабочим считаем
    канал, где у бота ЕСТЬ право приглашать; канал без прав не вытесняет уже
    настроенный.
    """
    with SessionLocal() as s:
        row = s.get(BotSetting, CHANNEL_KEY)
        current = row.value if row else None
        if current and not can_invite:
            return False, None
        if current == str(cid):
            return True, None
        if row is None:
            s.add(BotSetting(key=CHANNEL_KEY, value=str(cid),
                             updated_at=datetime.utcnow()))
        else:
            row.value, row.updated_at = str(cid), datetime.utcnow()
        s.commit()
        return True, current


def _sub(session, user) -> ChannelSubscriber:
    """Строка подписчика: одна на человека, создаётся при первом вопросе."""
    sub = session.query(ChannelSubscriber).filter_by(telegram_id=user.id).first()
    if sub is None:
        sub = ChannelSubscriber(telegram_id=user.id, username=user.username,
                                first_name=user.first_name, status="asked")
        session.add(sub)
        try:
            session.commit()
        except IntegrityError:
            # Гонка реальна: человек жмёт «Вступить» в канале и открывает бота
            # почти одновременно — два обработчика создают строку на один
            # telegram_id. Уникальный индекс не даёт задвоиться, берём чужую.
            session.rollback()
            sub = (session.query(ChannelSubscriber)
                   .filter_by(telegram_id=user.id).first())
    return sub


def _mark(telegram_id: int, **fields) -> None:
    """Точечно обновить строку подписчика. Нет строки — нечего обновлять:
    молча выходим, а не роняем выдачу доступа на AttributeError."""
    with SessionLocal() as s:
        sub = s.query(ChannelSubscriber).filter_by(telegram_id=telegram_id).first()
        if sub is None:
            return
        for key, value in fields.items():
            setattr(sub, key, value)
        s.commit()


def _age_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T.BTN_AGE_YES, callback_data="age:yes")],
        [InlineKeyboardButton(text=T.BTN_AGE_NO, callback_data="age:no")],
    ])


async def _notify_admin(bot: Bot, text: str) -> None:
    """Сообщение владельцу. Best-effort: сбой уведомления не ломает доступ."""
    if not settings.ADMIN_TG_ID:
        return
    try:
        await bot.send_message(settings.ADMIN_TG_ID, text,
                               disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("channel admin notify failed: %s", type(exc).__name__)


async def _bot_username(bot: Bot) -> str:
    try:
        me = await bot.get_me()
        return me.username or "bot"
    except Exception:  # noqa: BLE001 — только для текста сообщения
        return "bot"


async def ask_age(bot: Bot, chat_id_: int, user, source: str) -> None:
    """Задать вопрос про 18+. Вызывается и из deep-link, и из заявки.

    Для заявки chat_id_ — это `user_chat_id` из события: личный чат с человеком,
    писать в который Telegram разрешает, пока заявка не обработана.
    """
    with SessionLocal() as s:
        sub = _sub(s, user)
        sub.username, sub.first_name = user.username, user.first_name
        sub.source = source
        # Статус движется только вперёд: повторный вопрос не должен стирать
        # факт, что человек уже подтвердил возраст и получил ссылку.
        if sub.status in ("declined", "left"):
            sub.status = "asked"
        s.commit()
    await bot.send_message(chat_id_, T.AGE_ASK, reply_markup=_age_kb())


# ─── бота сделали админом канала: узнаём channel_id сам ──────────────────────

@router.my_chat_member(F.chat.type == "channel")
async def on_added_to_channel(ev: ChatMemberUpdated, bot: Bot) -> None:
    """Канал нельзя подключить из кода — админа назначает человек. Но когда
    назначит, Telegram присылает это событие: ловим id и проверяем права."""
    # У бота теперь ДВА канала: этот (18+) и клубный. Событие одно на оба, а
    # роутер привратника подключён первым — без явной передачи он записал бы
    # клубный канал как свой и увёл бы выдачу доступа 18+ в клуб.
    if settings.CLUB_CHANNEL_ID and str(ev.chat.id) == str(settings.CLUB_CHANNEL_ID):
        raise SkipHandler
    status = ev.new_chat_member.status
    if status in ("left", "kicked"):
        log.info("бот удалён из канала %s", ev.chat.id)
        return

    can_invite = bool(getattr(ev.new_chat_member, "can_invite_users", False))
    saved, replaced = _remember_channel(ev.chat.id, can_invite)
    log.info("бот подключён к каналу %s (%s), can_invite=%s, записан=%s",
             ev.chat.id, status, can_invite, saved)
    if not saved:
        # Канал без прав не вытесняет рабочий — но Николь должна знать, что
        # бота куда-то добавили, иначе она будет ждать доступ не оттуда.
        await _notify_admin(bot, T.CHANNEL_IGNORED.format(
            title=ev.chat.title or "без названия", channel_id=ev.chat.id))
        return
    rights = (T.CHANNEL_RIGHTS_OK.format(bot_username=await _bot_username(bot))
              if can_invite else T.CHANNEL_RIGHTS_MISSING)
    await _notify_admin(bot, T.CHANNEL_LINKED_ADMIN.format(
        title=ev.chat.title or "без названия", channel_id=ev.chat.id,
        rights=rights)
        + (T.CHANNEL_REPLACED.format(old=replaced) if replaced else ""))


@router.channel_post()
async def on_channel_post(msg: Message, bot: Bot) -> None:
    """Пост в канале — второй способ узнать канал, и он важнее, чем кажется.

    `my_chat_member` приходит ОДИН раз, в момент назначения админом. Если права
    боту выдали до релиза привратника, событие ушло в никуда, и канал остался бы
    неизвестным навсегда: снаружи всё выглядит настроенным, а доступ не выдаётся.
    Любой пост в канале это чинит — бот админ, значит посты он видит.

    ⚠️ Клубный канал сюда попадать не должен: иначе первый же пост Николь в
    клубе записал бы его как канал 18+ и увёл туда выдачу доступа.
    """
    if settings.CLUB_CHANNEL_ID and str(msg.chat.id) == str(settings.CLUB_CHANNEL_ID):
        return
    if channel_id():
        return
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=msg.chat.id, user_id=me.id)
    except Exception as exc:  # noqa: BLE001 — нет ответа, попробуем на следующем посте
        log.warning("channel_post: не смог проверить права (%s)", type(exc).__name__)
        return
    can_invite = bool(getattr(member, "can_invite_users", False))
    saved, _ = _remember_channel(msg.chat.id, can_invite)
    if not saved:
        return
    log.info("канал определён по посту: %s, can_invite=%s", msg.chat.id, can_invite)
    rights = (T.CHANNEL_RIGHTS_OK.format(bot_username=me.username or "bot")
              if can_invite else T.CHANNEL_RIGHTS_MISSING)
    await _notify_admin(bot, T.CHANNEL_LINKED_ADMIN.format(
        title=msg.chat.title or "без названия", channel_id=msg.chat.id, rights=rights))


async def channel_status(bot: Bot) -> str:
    """Что бот на самом деле знает о канале — для команды /channelstatus.

    Отвечает на три вопроса разом: какой канал считается рабочим, частный он или
    публичный, и хватает ли боту прав. Спрашиваем Telegram, а не свою базу:
    права могли снять уже после того, как бот их запомнил.
    """
    cid = channel_id()
    if not cid:
        return T.STATUS_NO_CHANNEL
    try:
        chat = await bot.get_chat(cid)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=cid, user_id=me.id)
    except Exception as exc:  # noqa: BLE001 — бота могли выгнать из канала
        return T.STATUS_ERROR.format(channel_id=cid, error=type(exc).__name__)
    can_invite = bool(getattr(member, "can_invite_users", False))
    return T.STATUS_OK.format(
        title=chat.title or "без названия",
        channel_id=cid,
        privacy=(T.STATUS_PUBLIC.format(username=chat.username) if chat.username
                 else T.STATUS_PRIVATE),
        role=member.status,
        rights=T.STATUS_RIGHTS_OK if can_invite else T.STATUS_RIGHTS_MISSING)


# ─── заявка на вступление в канал ────────────────────────────────────────────

@router.chat_join_request(F.chat.type == "channel")
async def on_join_request(ev: ChatJoinRequest, bot: Bot) -> None:
    """Заявка → вопрос в личку. Фильтр по типу чата намеренный: заявки в группу
    (чат участников интенсива) этот привратник не трогает."""
    # Клубный канал — не наш: если запомнить его здесь, привратник 18+ начнёт
    # раздавать доступ в платный клуб всем, кто подтвердил возраст.
    if settings.CLUB_CHANNEL_ID and str(ev.chat.id) == str(settings.CLUB_CHANNEL_ID):
        log.info("заявка в клубный канал — не мой поток")
        return
    known = channel_id()
    if known and str(ev.chat.id) != str(known):
        log.info("заявка в чужой канал %s — игнорирую", ev.chat.id)
        return
    if not known:
        # Бота могли сделать админом до этого релиза — тогда my_chat_member не
        # придёт никогда. Заявка сама подсказывает, что за канал.
        _remember_channel(ev.chat.id)

    # Откуда человек: Telegram кладёт в заявку ту самую пригласительную ссылку,
    # по которой он пришёл. Каждому сегменту рассылки — своя ссылка с именем
    # (`kommo1`, `kommo11`, `agents`), и источник считается сам, без utm.
    tag = clean_tag(getattr(ev.invite_link, "name", None) if ev.invite_link else None)
    source = f"jr:{tag}" if tag else "join_request"

    with SessionLocal() as s:
        sub = _sub(s, ev.from_user)
        sub.pending_request = True
        # Источник пишем и здесь: у подтвердивших возраст ask_age не вызовется,
        # а знать, из какой рассылки пришёл человек, надо всё равно. Метку
        # сегмента, если она уже стоит, не перетираем безымянной.
        if not sub.source or sub.source in ("join_request", "deeplink"):
            sub.source = source
        already_confirmed = sub.age_confirmed_at is not None
        s.commit()

    # Кто возраст уже подтверждал, второй раз не спрашиваем: согласие дано
    # однажды и лежит в базе с датой. Лишний вопрос — лишнее трение.
    if already_confirmed:
        await grant_access(bot, ev.from_user.id)
        return

    try:
        await ask_age(bot, ev.user_chat_id, ev.from_user, source)
    except Exception as exc:  # noqa: BLE001 — человек мог заблокировать бота
        log.warning("join request %s: вопрос не доставлен (%s)",
                    ev.from_user.id, type(exc).__name__)
        who = (f"@{ev.from_user.username}" if ev.from_user.username
               else f"id{ev.from_user.id}")
        await _notify_admin(bot, T.ADMIN_ASK_FAILED.format(
            who=who, error=type(exc).__name__))


# ─── ответ на вопрос ─────────────────────────────────────────────────────────

async def _drop_buttons(call: CallbackQuery) -> None:
    """Убрать кнопки у заданного вопроса. Двойное нажатие «Да» иначе способно
    выдать две ссылки — а ещё висящие кнопки выглядят как «ответ не принят»."""
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 — сообщение старое или уже правлено
        log.debug("edit_reply_markup: %s", type(exc).__name__)


@router.callback_query(F.data == "age:no")
async def cb_age_no(call: CallbackQuery, bot: Bot) -> None:
    """«Нет» — доступ не даём и висящую заявку отклоняем. Отказ не пожизненный:
    ошибиться кнопкой легко, повторный /channel снова задаст вопрос."""
    await _drop_buttons(call)
    with SessionLocal() as s:
        sub = _sub(s, call.from_user)
        pending, sub.pending_request = sub.pending_request, False
        if sub.status != "in_channel":
            sub.status = "declined"
        s.commit()

    cid = channel_id()
    if pending and cid:
        try:
            await bot.decline_chat_join_request(chat_id=cid,
                                                user_id=call.from_user.id)
        except Exception as exc:  # noqa: BLE001 — заявку могли уже обработать
            log.warning("decline %s: %s", call.from_user.id, type(exc).__name__)
    # Пишем человеку напрямую, а не через call.message: у старого сообщения
    # ответить может быть уже нельзя, а сказать «нет так нет» мы обязаны.
    await bot.send_message(call.from_user.id, T.AGE_NO)
    await call.answer()


@router.callback_query(F.data == "age:yes")
async def cb_age_yes(call: CallbackQuery, bot: Bot) -> None:
    await _drop_buttons(call)
    with SessionLocal() as s:
        sub = _sub(s, call.from_user)
        if sub.age_confirmed_at is None:
            sub.age_confirmed_at = datetime.utcnow()
        # Только вперёд: у того, кому ссылку уже выдали (invited) или кто уже
        # внутри, повторное «да» не должно откатывать путь назад.
        if sub.status in ("asked", "declined", "left"):
            sub.status = "confirmed"
        s.commit()
    await grant_access(bot, call.from_user.id)
    await call.answer()


@router.chat_member(F.chat.type == "channel")
async def on_channel_member(ev: ChatMemberUpdated) -> None:
    """Человек вошёл в канал или вышел из него.

    Нужно, чтобы статус в базе отражал факт, а не намерение: «ссылку выдали» и
    «человек внутри» — разные вещи, и в отчёте их путать нельзя. Новых строк не
    создаём: таблица про подтверждения возраста, а не про всех подписчиков.
    """
    known = channel_id()
    if known and str(ev.chat.id) != str(known):
        # Не «не наш канал, забыли», а «не наш — отдай следующему»: входы и
        # выходы в клубном канале ждёт club.on_channel_member.
        raise SkipHandler
    status = ev.new_chat_member.status
    with SessionLocal() as s:
        sub = (s.query(ChannelSubscriber)
               .filter_by(telegram_id=ev.from_user.id).first())
        if sub is None:
            return
        if status in IN_CHANNEL_STATUSES:
            sub.status = "in_channel"
        elif status in ("left", "kicked"):
            sub.status = "left"
        s.commit()


async def grant_access(bot: Bot, telegram_id: int) -> None:
    """Открыть канал подтвердившему возраст.

    Порядок важен: сначала пробуем одобрить висящую заявку (человек уже стоит в
    очереди — ссылка ему не нужна), и только потом выдаём персональную ссылку.
    Идемпотентно: свежая ссылка переиспользуется, вторую не плодим.
    """
    cid = channel_id()
    if not cid:
        await bot.send_message(telegram_id, T.NO_CHANNEL_YET)
        await _notify_admin(bot, T.ADMIN_NO_CHANNEL.format(
            bot_username=await _bot_username(bot)))
        return

    with SessionLocal() as s:
        sub = s.query(ChannelSubscriber).filter_by(telegram_id=telegram_id).first()
        pending = bool(sub and sub.pending_request)
        fresh_link = None
        if sub and sub.invite_link and sub.invited_at and (
                datetime.utcnow() - sub.invited_at < timedelta(hours=LINK_TTL_HOURS)):
            fresh_link = sub.invite_link

    # Висит заявка — одобряем её: это дешевле ссылки и не оставляет хвостов.
    if pending:
        try:
            await bot.approve_chat_join_request(chat_id=cid, user_id=telegram_id)
            _mark(telegram_id, pending_request=False, status="in_channel")
            await bot.send_message(telegram_id, T.ACCESS_APPROVED)
            return
        except Exception as exc:  # noqa: BLE001 — заявка протухла или уже в канале
            log.info("approve %s не прошёл (%s) → выдам ссылку",
                     telegram_id, type(exc).__name__)
            _mark(telegram_id, pending_request=False)

    # Уже подписан — ссылку не тратим.
    try:
        member = await bot.get_chat_member(chat_id=cid, user_id=telegram_id)
        if member.status in IN_CHANNEL_STATUSES:
            _mark(telegram_id, status="in_channel")
            await bot.send_message(telegram_id, T.ALREADY_IN)
            return
    except Exception as exc:  # noqa: BLE001 — нет ответа → считаем, что не в канале
        log.info("get_chat_member %s: %s", telegram_id, type(exc).__name__)

    if fresh_link:
        await bot.send_message(telegram_id, T.ACCESS_LINK.format(link=fresh_link),
                               disable_web_page_preview=True)
        return

    try:
        # Срок жизни ссылки передаём интервалом, а не датой. Проверено:
        # aiogram сериализует naive datetime как ЛОКАЛЬНОЕ время, и на машине с
        # часовым поясом Дубая (+4) ссылка протухала бы на 4 часа раньше срока.
        link = await bot.create_chat_invite_link(
            chat_id=cid, member_limit=1, name=f"age18-{telegram_id}",
            expire_date=timedelta(hours=LINK_TTL_HOURS))
    except Exception as exc:  # noqa: BLE001 — нет прав или Telegram ответил ошибкой
        log.error("invite в канал для %s: %s", telegram_id, type(exc).__name__)
        await bot.send_message(telegram_id, T.LINK_FAILED)
        await _notify_admin(bot, T.ADMIN_LINK_FAILED.format(error=type(exc).__name__))
        return

    # Именно `invited`, а не `in_channel`: ссылку выдали — вошёл ли человек, мы
    # ещё не знаем. Отметку о входе поставит событие chat_member.
    _mark(telegram_id, invite_link=link.invite_link,
          invited_at=datetime.utcnow(), status="invited")
    await bot.send_message(telegram_id, T.ACCESS_LINK.format(link=link.invite_link),
                           disable_web_page_preview=True)


# ─── счётчики по меткам рассылки: наружу уезжают только цифры ────────────────

# Все статусы, которые ставит привратник (`asked` → `confirmed` → `invited` →
# `in_channel` / `left`, плюс `declined`). Человек всегда ровно в одном из них,
# поэтому сумма шести чисел — это все, кто пришёл с меткой.
TAG_STATUSES = ("asked", "confirmed", "invited", "in_channel", "left", "declined")


def iso_utc(moment: datetime | None) -> str | None:
    """Время наружу: UTC с явной «Z». В базе лежит naive-UTC (`datetime.utcnow`),
    и без буквы принимающая сторона прочитала бы его как своё местное — на
    карточке рассылки рассылка «пришла» на несколько часов раньше или позже."""
    return None if moment is None else moment.replace(microsecond=0).isoformat() + "Z"


def tag_counts(session, *, prefix: str = "dl:",
               since: datetime | None = None) -> list[dict]:
    """Сколько людей с каждой меткой рассылки в каком статусе — на сейчас.

    Зачем: рассылка ведёт человека ссылкой `?start=channel-<код>`, бот пишет
    метку `dl:<код>` и дальше знает то, чего не знает отправитель рассылки, —
    дошёл ли человек до канала и остался ли там. Это окно наружу, и в него
    видны ТОЛЬКО метка, числа и две даты: ни `telegram_id`, ни имени, ни
    пригласительной ссылки. Забирает их чужой сервис, ПД ему не нужны.

    Одним запросом с группировкой: строк в таблице столько, сколько людей прошло
    привратника, и тянуть их в память ради шести счётчиков незачем.

    `first_seen` — когда метка привела первого. `last_seen` — последнее известное
    движение: `updated_at` в таблице нет, поэтому берём самую позднюю из трёх дат
    строки. `since` отсекает по дате ПРИХОДА, а не по движению: вопрос «что дала
    рассылка» — про тех, кто пришёл после неё.

    Только чтение: ни одной записи эта дорога не меняет.
    """
    sub = ChannelSubscriber
    started = func.min(sub.created_at)
    moved = func.max(func.coalesce(sub.invited_at, sub.age_confirmed_at,
                                   sub.created_at))
    stmt = (
        select(sub.source,
               *[func.count(case((sub.status == st, sub.id))) for st in TAG_STATUSES],
               started, moved)
        # autoescape: префикс приходит снаружи, а `%` и `_` в LIKE —
        # подстановочные знаки. Без экранирования `prefix=%` вернул бы заодно
        # заявки из канала (`jr:`), то есть не то, что просили.
        .where(sub.source.startswith(prefix, autoescape=True))
        .group_by(sub.source)
        .order_by(started.desc(), sub.source)   # свежая рассылка сверху
    )
    if since is not None:
        stmt = stmt.where(sub.created_at >= since)
    return [
        {"tag": source, **dict(zip(TAG_STATUSES, counts)),
         "first_seen": iso_utc(first_seen), "last_seen": iso_utc(last_seen)}
        for source, *counts, first_seen, last_seen in session.execute(stmt)
    ]
