from datetime import datetime
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Partner(Base):
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # telegram_id больше не обязателен: партнёр может зарегистрироваться по email
    # без Telegram (план 2026-05-23). Postgres unique-индекс допускает несколько
    # NULL, поэтому уникальность для TG-партнёров сохраняется.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    photo_url: Mapped[str | None] = mapped_column(String(512))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    segment: Mapped[str | None] = mapped_column(String(32))
    ref_slug: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    # Язык интерфейса бота: "ru"/"en". None → не выбран явно, бот берёт по
    # language_code Telegram (см. resolve_lang в bot.py). Кнопка-переключатель
    # проставляет сюда явный выбор.
    lang: Mapped[str | None] = mapped_column(String(2))
    tier: Mapped[str] = mapped_column(String(16), default="bronze")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # Доход партнёра 2-го уровня (AED) — суммарная комиссия за приведённых им
    # суб-агентов (решение Николь 2026-07-21; напр. Ostrovok: 367 Куришко + 315
    # Salimkhan = 682). НЕ привязано к лидам (это % от чужих сделок), поэтому
    # храним общей суммой на партнёре и вручную обновляем из комиссионного Excel.
    # Прибавляется к «Заработано» на дашборде/полосе. NULL = 0.
    l2_income_aed: Mapped[float | None] = mapped_column(Numeric(12, 2))
    # Разбивка дохода 2-го уровня по суб-агентам (решение Николь 2026-07-21):
    # список [{"name": "Илья Куришко", "aed": 367}, ...]. В кабинете строка
    # «2-й уровень» разворачивается в имена суб-агентов с их суммами, справа —
    # общая (сумма списка). Ручной ввод из комиссионного Excel. NULL/[] = не
    # показываем. Пришло на смену голому числу l2_income_aed (оно оставлено для
    # совместимости; «Заработано» берёт сумму из списка, а если списка нет — число).
    l2_income: Mapped[list | None] = mapped_column(JSON)
    # Связь с агентом в Kommo: enum_id значения поля «ID AGENT» (#961886) воронки 1.1.
    # Один Partner ↔ один Kommo-агент. По нему отчёт/дайджест тянут лиды агента.
    # kommo_agent_name — кэш отображаемого имени (латиница), для писем/дайджеста.
    kommo_agent_enum_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    kommo_agent_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime)
    links_viewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    products_viewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Шаг чеклиста «Посмотрите 2 видео»: заполняется при первом заходе в /courses.
    courses_viewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    checklist_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Анкета партнёра (Фаза L, план 2026-05-27): профиль для подбора
    # СТРАТЕГИЧЕСКОГО партнёрства (это анкета самого партнёра, НЕ онбординг
    # клиента). Ответы — только варианты из белого списка (JSON-словарь);
    # survey_completed_at IS NOT NULL → анкета пройдена (баннер скрыт).
    # ⚠️ ПД («опасная тройка»): реквизиты выплат (номера карт/кошельков/IBAN)
    # сюда НЕ пишем — только ТИП канала. Точные реквизиты менеджер собирает
    # в личной переписке, вне БД.
    onboarding_answers: Mapped[dict | None] = mapped_column(JSON)
    survey_completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    referrals: Mapped[list["Referral"]] = relationship(back_populates="partner")
    leads: Mapped[list["Lead"]] = relationship(back_populates="partner")


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    ref_slug: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(16))
    visitor_meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    partner: Mapped[Partner] = relationship(back_populates="referrals")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    client_name: Mapped[str] = mapped_column(String(255))
    client_phone: Mapped[str | None] = mapped_column(String(32))
    client_telegram: Mapped[str | None] = mapped_column(String(64))
    client_email: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str | None] = mapped_column(String(255))
    task_description: Mapped[str | None] = mapped_column(Text)
    # «Что НЕ предлагать клиенту» — обязательное поле в /transfer (Фаза F, план
    # 2026-05-27): партнёр пишет ограничения/табу, чтобы менеджер не повредил
    # его репутации лишним предложением. nullable=True, потому что лиды из
    # kommo_sync и легаси-ТГ-бота этого поля не заполняют.
    do_not_offer: Mapped[str | None] = mapped_column(Text)
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    amount_aed: Mapped[float | None] = mapped_column(Numeric(12, 2))
    # Статус партнёрского вознаграждения по ВЫИГРАННОМУ лиду (Фаза B, план
    # 2026-05-27): in_calc / to_pay / paid. Менеджер ставит вручную
    # (scripts/set_payout_state.py). NULL у won-лида = «в расчёте» — дефолт
    # выводим на слое отображения (payout_label в main.py), в данные не пишем,
    # поэтому колонка nullable без DB-default. Таблицу Payout НЕ заводим
    # (решение Николь). kommo_sync это поле не трогает — ручная отметка живёт.
    payout_state: Mapped[str | None] = mapped_column(String(16))
    # Комиссия партнёра по ЭТОЙ сделке в AED (решение Николь 2026-07-21).
    # Ставки единой нет: вознаграждение согласуется по сделке (у Dubru за Павла —
    # 550 AED при чеке 1732), поэтому храним суммой, а не выводим из amount_aed.
    # Проставляется вручную менеджером; kommo_sync это поле не трогает, как и
    # payout_state. NULL = «ещё не посчитана» — в «Заработано» даёт вклад 0.
    commission_aed: Mapped[float | None] = mapped_column(Numeric(12, 2))
    # Якорь выплаты (Фаза K, план 2026-05-27): момент ПЕРВОГО перехода лида в
    # `won`, ставится один раз в kommo_sync и больше не двигается. Из него
    # payout_due_date() считает «10-е число следующего месяца». НЕ используем
    # updated_at — он шевелится при любом синке ([[project_lead_updated_at_tech_debt]]),
    # а дата выплаты должна быть стабильной. nullable: старые/не-won лиды — без него.
    won_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Идемпотентность win-пуша (Фаза K): отметка, что событие «клиент оплатил»
    # уже ОБРАБОТАНО (пуш отправлен / dry-run / бэкфилл). NULL → ещё не обработан.
    # Ставится один раз; повторный переход в won второго пуша не плодит. Важно:
    # в dry-режиме тоже штампуется (событие «прожито»), чтобы при go-live не
    # ушла лавина пушей по старым оплатам ([[feedback_no_agent_outreach_yet]]).
    won_notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ─── Модуль выплат (план 2026-06-02, замена Excel менеджера). Заполняет
    # менеджер на /admin/payouts. Под-таблицу НЕ заводим (решение Николь).
    # fee_aed — комиссия агента; payout_urgent — «срочно» (агенту НЕ показываем,
    # это менеджерский флаг поверх payout_state); agreement_url/receipt_url —
    # ссылки Google Drive; bank_details — реквизиты (финансовые ПД, видны ТОЛЬКО
    # админу на /admin/*); payout_paid_on — месяц/дата выплаты (свободный текст,
    # как в файле менеджера: «September», «31 December»).
    fee_aed: Mapped[float | None] = mapped_column(Numeric(12, 2))
    payout_urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    agreement_url: Mapped[str | None] = mapped_column(Text)
    bank_details: Mapped[str | None] = mapped_column(Text)
    payout_receipt_url: Mapped[str | None] = mapped_column(Text)
    payout_paid_on: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    partner: Mapped[Partner] = relationship(back_populates="leads")


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    segment: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    body_md: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # EN-версии (рендерятся при lang=en, fallback на русское поле). Пусто → русский.
    segment_en: Mapped[str | None] = mapped_column(String(64))
    title_en: Mapped[str | None] = mapped_column(String(255))
    body_md_en: Mapped[str | None] = mapped_column(Text)
    # Креатив к тексту (2026-07-23): партнёр публикует пост картинкой + текстом.
    # image_path — файл, который партнёр СКАЧИВАЕТ (полный размер, /static/img/…);
    # image_thumb — лёгкое превью для карточки в кабинете. NULL → текст без картинки
    # (старое поведение всех остальных шаблонов).
    image_path: Mapped[str | None] = mapped_column(String(255))
    image_thumb: Mapped[str | None] = mapped_column(String(255))
    # Тип ПАРТНЁРА, под который собран ассет (Фаза C, план 2026-05-27): ключ из
    # PARTNER_TYPES в main.py (employee/solo/events/agency/media/consultant/insider).
    # NULL → шаблон не привязан к типу (генерик-крючки /messages — старое поведение,
    # они НЕ показываются в /kits). Это ДРУГАЯ ось, чем segment (= тип ассета:
    # «Интро WhatsApp» / «Lead-магнит» / «Disclosure»…). EN-зеркало не нужно:
    # partner_type — внутренний ключ, ярлык берётся из PARTNER_TYPES (ru/en).
    partner_type: Mapped[str | None] = mapped_column(String(32), index=True)
    # Способ привлечения, под который собран текст (план 2026-06-02 «переборка
    # /tools по способам»). Ключ из METHODS в main.py (broadcast/social/event/
    # leadmagnet/intro/directlinks). Это ось ГРУППИРОВКИ на /tools вместо
    # partner_type (тип партнёра как ось убран по решению Николь). NULL → текст
    # не показывается в новых вкладках /tools (мягкая деградация).
    method: Mapped[str | None] = mapped_column(String(32), index=True)
    # Какую персональную ссылку партнёра вшивать вместо плейсхолдера {link} в теле
    # (resolve в main._personal_links): consult_quiz/consult_tg/consult_wa/
    # mk_quiz/mk_tg/mk_wa/partner_bot. NULL → в теле нет {link} (напр. insider-
    # тексты с «голым» wa.me для дискретности — намеренно без трекинг-ссылки).
    link_key: Mapped[str | None] = mapped_column(String(16))


class ProductBlock(Base):
    __tablename__ = "product_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    price_aed: Mapped[str | None] = mapped_column(Text)
    summary_md: Mapped[str] = mapped_column(Text)
    full_md: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # EN-версии (рендерятся при lang=en, fallback на русское поле). Пусто → русский.
    title_en: Mapped[str | None] = mapped_column(String(255))
    price_aed_en: Mapped[str | None] = mapped_column(Text)
    summary_md_en: Mapped[str | None] = mapped_column(Text)
    full_md_en: Mapped[str | None] = mapped_column(Text)


class FaqItem(Base):
    __tablename__ = "faq_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    question: Mapped[str] = mapped_column(String(500))
    answer_md: Mapped[str] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # EN-версии (рендерятся при lang=en, fallback на русское поле). Пусто → русский.
    category_en: Mapped[str | None] = mapped_column(String(64))
    question_en: Mapped[str | None] = mapped_column(String(500))
    answer_md_en: Mapped[str | None] = mapped_column(Text)


class Course(Base):
    """Обучающий курс для партнёра в ЛК (раздел «Курсы»).

    Карточка-витрина: заголовок, подзаголовок (длительность/шаги), строка «Итог»,
    полоса прогресса и одна CTA-кнопка. Контент редактируется через seed
    (force-reseed, как ProductBlock/FaqItem).

    Прогресс пока «только вид»: progress_steps — фиксированное значение из данных,
    не пер-партнёрский трекинг. Статус кнопки выводится из progress_steps/total_steps:
    0 → «Начать», 0<x<total → «Продолжить», x>=total → done_label.
    """
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    subtitle: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str | None] = mapped_column(Text)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    progress_steps: Mapped[int] = mapped_column(Integer, default=0)
    done_label: Mapped[str] = mapped_column(String(64), default="Завершено")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Английские версии полей витрины (рендерятся при lang=en). Заложены заранее,
    # чтобы таблица создалась сразу со всеми колонками: create_all НЕ умеет ALTER.
    # Пусто → шаблон откатывается на русское поле (graceful fallback).
    title_en: Mapped[str | None] = mapped_column(String(255))
    subtitle_en: Mapped[str | None] = mapped_column(String(255))
    outcome_en: Mapped[str | None] = mapped_column(Text)
    done_label_en: Mapped[str | None] = mapped_column(String(64))


class LoginSession(Base):
    """Однократный токен для входа в ЛК через бота (deep-link auth)."""
    __tablename__ = "login_sessions"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Если вход начат по персональной инвайт-ссылке /invite/<slug> (Фаза 0.7),
    # сюда кладётся ref_slug пред-созданного Partner-агента — чтобы привязать
    # telegram_id к НЕМУ, а не плодить дубль.
    ref_slug: Mapped[str | None] = mapped_column(String(16))


class EmailLoginToken(Base):
    """Одноразовый токен для входа в ЛК по email (магическая ссылка, план 2026-05-23).

    Партнёр запрашивает вход → сюда пишется криптослучайный token + email.
    Письмо со ссылкой `…/auth/email/callback?token=…` уходит через Resend. Клик:
    токен валиден (не использован, не истёк, TTL 15 мин) → выдаём JWT-cookie.
    consumed_at делает токен одноразовым; старые невостребованные чистятся в startup.
    """
    __tablename__ = "email_login_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ref_slug пред-созданного Partner-агента, если вход начат по инвайт-ссылке (Фаза 0.7).
    ref_slug: Mapped[str | None] = mapped_column(String(16))


class PartnerIdentity(Base):
    """Идентификатор агента для входа (план 2026-05-27, Вариант А).

    Один кабинет (Partner) ↔ много идентификаторов разного типа:
    - `kind="phone"`     — номер (digits-only, как normalize_phone) для входа по WhatsApp-коду;
    - `kind="tg_username"` — username Telegram (lower, без `@`) — доверенные ники канала.

    Нужно, потому что у агента бывает несколько номеров, а у канала-корзины
    (`4dev`, `Ilya+Andrey`…) — массив номеров и ников команды. Матч на входе идёт
    по этой таблице (value → partner_id), `Partner.phone` остаётся как основной/совместимость.

    value уникален В РАМКАХ kind (один номер/ник не ведёт в два кабинета).
    Заполняется из `dumps/agent_phone_map.json` (phone) + ручных от Николь
    (`agent_phone_manual.json`: phones[]/tg_usernames[]) — Фаза 1б, на Railway.
    """
    __tablename__ = "partner_identities"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_identity_kind_value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    value: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    partner: Mapped["Partner"] = relationship()


class PhoneLoginToken(Base):
    """Одноразовый код для входа в ЛК по номеру телефона (план 2026-05-27).

    Телефон — сквозной идентификатор агента (WhatsApp = телефон, карточка
    воронки 6 = телефон), поэтому вход по номеру одновременно аутентифицирует и
    объединяет каналы. Партнёр вводит номер → сюда пишется hmac-хэш 6-значного
    кода + нормализованный (digits-only) телефон. Код уходит в WhatsApp через
    Wazzup. На вводе кода: не истёк (TTL 10 мин), не использован, ≤5 попыток →
    выдаём JWT-cookie.

    Безопасность (опасная тройка — персональные данные): хранится ТОЛЬКО хэш кода
    (hmac-sha256 с JWT_SECRET-перцем), сам код и телефон НЕ логируются. attempts
    режет брутфорс, consumed_at делает код одноразовым, протухшие чистятся в
    startup. phone здесь — не PK: на номер может быть несколько запросов, verify
    берёт последний невостребованный.
    """
    __tablename__ = "phone_login_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationAttempt(Base):
    """Журнал попыток уведомить партнёра (Фаза K, план 2026-05-27).

    Append-only аудит КАЖДОГО триггера (digest / win-пуш) — даже когда наружу
    ничего не ушло. Нужен для трёх вещей:
    - доказать, что при NOTIFICATIONS_LIVE=false реальных отправок 0 (все строки
      status='dry_run');
    - идемпотентность: один digest в день на партнёра (партнёр+kind+дата);
    - уникальность текста: выбор шапки/концовки сверяется с прошлой записью.

    Безопасность («опасная тройка»): `recipient` хранится МАСКИРОВАННЫМ (код страны
    + 2 последние цифры для WA, либо tg:<id> — это наш внутренний chat_id, не ПД
    клиента). `body` — полный текст сообщения ПАРТНЁРУ (агрегаты в digest, имя
    собственного клиента партнёра в win — не чужие ПД). В общий лог пишем только
    partner_id/kind/status/тип-ошибки, не телефон и не текст.
    """
    __tablename__ = "notification_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    # 'digest' | 'win'
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # 'tg' | 'wa' | 'none'
    channel: Mapped[str] = mapped_column(String(8))
    recipient: Mapped[str | None] = mapped_column(String(64))  # маскированный
    body: Mapped[str] = mapped_column(Text)
    # 'dry_run' | 'sent' | 'failed' | 'no_channel' | 'rate_limited'
    status: Mapped[str] = mapped_column(String(16), index=True)
    error_short: Mapped[str | None] = mapped_column(String(64))
    # Привязка к конкретному лиду для win-пуша (для digest — NULL).
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class EventRegistration(Base):
    """Регистрации на мастер-классы и события — наследник telegram-bot-2brain."""
    __tablename__ = "event_registrations"
    __table_args__ = (UniqueConstraint("telegram_id", "event_slug", name="uq_event_per_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event_slug: Mapped[str] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64))
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    attended: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict | None] = mapped_column(JSON)


class QuizSubmission(Base):
    """Заявки с квиз-лендинга /consultation (план 2026-06-02).

    Публичный квиз: 3 вопроса с вариантами → имя + телефон. Каждая заявка пишется
    сюда (источник правды НЕЗАВИСИМО от Kommo) и — под предохранителем
    settings.QUIZ_KOMMO_LIVE — уходит лидом в Kommo воронку 1.1.

    Атрибуция к агенту: `ref_slug` из ссылки (?ref=<slug>) → `Partner.ref_slug` →
    `partner_id` + `Partner.kommo_agent_enum_id` (на лиде ставится поле «ID AGENT»
    #961886). Дальше существующий kommo_sync сам привяжет лид к партнёру.

    Безопасность («опасная тройка»: ПД клиента + отправка наружу): `phone`/`name` —
    ПД, в общий лог пишем только маску телефона + статус, не сырой ввод. `answers`
    и UTM — наши данные, не ПД. Сырой пользовательский ввод НЕ рендерим в HTML.
    """
    __tablename__ = "quiz_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32), index=True)  # normalize_phone (digits-only)
    # Дискриминатор лендинга: NULL = квиз /consultation (по умолчанию), либо slug
    # события (напр. 'mk-buh-2026-06-11' — регистрация на мастер-класс). Позволяет
    # отделять регистрации МК от заявок-консультаций в одной таблице (план 2026-06-02).
    event_slug: Mapped[str | None] = mapped_column(String(64), index=True)
    # Ответы белого списка: {"service": "...", "company": "...", "timing": "..."}.
    answers: Mapped[dict | None] = mapped_column(JSON)
    # Атрибуция агента
    ref_slug: Mapped[str | None] = mapped_column(String(16), index=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), index=True)
    # UTM/источник трафика (для аналитики даже когда агента в метке нет)
    utm_source: Mapped[str | None] = mapped_column(String(128))
    utm_medium: Mapped[str | None] = mapped_column(String(128))
    utm_campaign: Mapped[str | None] = mapped_column(String(128))
    utm_content: Mapped[str | None] = mapped_column(String(128))
    utm_term: Mapped[str | None] = mapped_column(String(128))
    referrer: Mapped[str | None] = mapped_column(Text)
    landing_url: Mapped[str | None] = mapped_column(Text)
    # Kommo: 'pending' (ещё не обрабатывали) | 'dry' (гард off, в сеть не ходили) |
    # 'sent' (лид создан) | 'failed' (ошибка API). kommo_lead_id — id созданного лида.
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger)
    kommo_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class LinkClick(Base):
    """Переход по персональной ссылке агента (план 2026-07-23).

    Клик = вход ЧЕЛОВЕКА по публичной ссылке: лендинг-квиз (/consultation, /mk,
    /guide/*) или редирект в чат/бот (/ct, /cw, /mt, /mw, /p). Недостающая
    координата аналитики: QuizSubmission ловит ЛИДА, LinkClick — ПЕРЕХОД; вместе
    дают конверсию и сигнал «ссылка перестала собирать переходы». Концептуально это
    «Событие kind='visit'» (architecture/03-данные), реализовано конкретной таблицей
    в стиле кода. Append-only, не обновляется. Пишет app/linkstat.record_click
    (синхронный best-effort INSERT — не блокирует и не роняет ответ).

    ПД-минимизация («опасная тройка»): пишем ТОЛЬКО контент + канал + ref_slug +
    время. НЕ пишем IP, query-строку (там бывают токены входа), User-Agent, сырой
    ввод. User-Agent app/linkstat читает в памяти лишь чтобы отсеять превью-краулеры
    мессенджеров и self-пробу монитора — в таблицу он не попадает.
    """
    __tablename__ = "link_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Что за ссылкой (ось «какой чек-лист/мастер-класс»): consultation | mk |
    # leadmagnet_corptax | leadmagnet_5mistakes | partner_bot (linkstat.CONTENT_KEYS).
    content_key: Mapped[str] = mapped_column(String(32), index=True)
    # Как открыли: quiz (страница-лендинг) | tg | wa | bot. VARCHAR(16) с запасом.
    surface: Mapped[str] = mapped_column(String(16))
    # Реф-метка агента (обрезана до 16, как Partner.ref_slug — иначе value too long → 502).
    ref_slug: Mapped[str | None] = mapped_column(String(16), index=True)
    # Резолвится по ref_slug в момент записи (best-effort). NULL — метки нет или агент не найден.
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class HealthAlert(Base):
    """Дедуп отправленных алертов о поломке ссылок (план 2026-07-23).

    Append-only. Одна строка = один факт отправки алерта Николь по issue_key. Дедуп
    «не чаще раза в сутки на issue_key». Строку пишем ТОЛЬКО ПОСЛЕ подтверждённой
    отправки в TG — иначе транзиентный сбой отправки заглушил бы инцидент на сутки.
    issue_key гранулярный — 'детектор:таргет:причина' (напр. 'landings_down:/mk:404').
    """
    __tablename__ = "health_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(String(160), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PageView(Base):
    """Заход залогиненного агента на страницу кабинета (план 2026-06-03).

    Зачем: показать Николь, какие разделы портала агенты реально открывают
    (что живое, что мёртвое, удерживает ли кабинет после первого лида). До этой
    таблицы поведение нигде не писалось — был только Partner.last_login_at.

    ПД-минимизация («опасная тройка»): храним ТОЛЬКО кто (partner_id) + что
    (нормализованный path + section) + когда. НЕ пишем query-строку (там бывают
    токены входа), IP, содержимое. Доступ к агрегатам — только админ (require_admin).

    Ограничение v1 (решение Николь 2026-06-03): это «открытие страницы», НЕ факт
    действия — заход на /tools ≠ скопировал ссылку. Действия — кандидат в v2.
    """
    __tablename__ = "page_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), index=True)
    # Нормализованный путь: динамические сегменты схлопнуты
    # (/courses/abc/day/2 → /courses/:slug/day/:day) — иначе агрегаты рассыплются.
    path: Mapped[str] = mapped_column(String(128), index=True)
    # Человеко-понятная функция кабинета (см. usage.SECTION_LABELS):
    # "dashboard"|"leads"|"tools"|"kb"|"courses"|"transfer"|"account"|"onboarding".
    section: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PaymentClaim(Base):
    """Заявление об оплате со страницы /pay (план 2026-08-03).

    Это НЕ платёж и не его подтверждение: платёжного слоя в ядре нет, деньги
    собираются вне системы. Человек увидел реквизиты, перевёл и нажал «Я оплатил» —
    здесь лежит его слово, а факт зачисления сверяет человек по выписке. Отсюда
    `status`: заявление живёт как 'new', пока кто-то не отметит 'confirmed'/'rejected'.

    Почему отдельная таблица, а не quiz_submissions: там ЛИДЫ (кто-то хочет, чтобы
    ему позвонили), а здесь ДЕНЬГИ — другой жизненный цикл, другая срочность и
    сверка с выпиской. Смешивать их в одной таблице значит потерять оба смысла.

    Безопасность («опасная тройка»: ПД клиента + отправка наружу): `name`/`phone`/
    `contact` — ПД, в лог идёт только маска телефона. Реквизиты плательщика (номер
    его карты, хеш транзакции) мы НЕ спрашиваем и не храним — нам достаточно суммы
    и времени, чтобы найти платёж в выписке. `amount_label` пишем из pay_config, а
    не из формы: сумму в нашей записи клиент подставлять не должен.
    """
    __tablename__ = "payment_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Что оплачивали — pay_config.PRODUCT["slug"] (дискриминатор потоков/продуктов).
    product_slug: Mapped[str] = mapped_column(String(64), index=True)
    # Способ: 'rub' | 'card' | 'crypto' — строго из pay_config.VALID_METHODS.
    method: Mapped[str] = mapped_column(String(16), index=True)
    # Снимок цены, которая была показана на странице для этого способа. Именно
    # снимок: цена в конфиге завтра поменяется, а сверять выписку надо по той,
    # что человек видел сегодня.
    amount_label: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32), index=True)  # normalize_phone (digits-only)
    # Дополнительный контакт для доступа: @telegram или email. Свободный ввод —
    # в HTML не рендерим (анти-XSS), в Telegram уходит как текст.
    contact: Mapped[str | None] = mapped_column(String(200))
    # Комментарий плательщика («платил не со своей карты» и т.п.).
    note: Mapped[str | None] = mapped_column(Text)
    # Атрибуция агента — та же схема, что у QuizSubmission (?ref=<slug>).
    ref_slug: Mapped[str | None] = mapped_column(String(16), index=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), index=True)
    utm_source: Mapped[str | None] = mapped_column(String(128))
    utm_medium: Mapped[str | None] = mapped_column(String(128))
    utm_campaign: Mapped[str | None] = mapped_column(String(128))
    utm_content: Mapped[str | None] = mapped_column(String(128))
    utm_term: Mapped[str | None] = mapped_column(String(128))
    referrer: Mapped[str | None] = mapped_column(Text)
    landing_url: Mapped[str | None] = mapped_column(Text)
    # 'new' — сказал, что оплатил | 'confirmed' — нашли в выписке | 'rejected' — не нашли.
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class IntensiveLead(Base):
    """Человек в боте оплат интенсива @Nikol_hilton_bot (план 2026-08-03).

    Отличие от PaymentClaim: тот хранит ЗАЯВЛЕНИЕ об оплате со страницы /pay
    («я оплатил», проверяет человек). Здесь — путь человека в боте, где оплату
    подтверждает Lava по API, а доступ в чат выдаётся автоматически. Одна строка
    живёт от первого /start до входа в чат, статус двигается по этому пути.

    Безопасность («опасная тройка»): `telegram_id`, имя и username — ПД. В лог
    пишем только id, в промт модели ПД не уходят (мост обезличивания). Инвайт в
    чат одноразовый и привязан к конкретному человеку — общую ссылку не раздаём.
    """
    __tablename__ = "intensive_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    # Путь: new → invoiced (счёт выставлен) → paid (Lava подтвердила) →
    # in_chat (инвайт выдан). refused — сказал «не буду».
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    # Что именно покупают: 'first_day' (первый день) или 'intensive' (весь курс).
    # См. lava.PRODUCTS. NULL у старых строк — считаем интенсивом.
    product_code: Mapped[str | None] = mapped_column(String(16))
    # Счёт в Lava: id и ссылка. По contractId сверяем оплату через /api/v1/sales.
    lava_invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    lava_invoice_url: Mapped[str | None] = mapped_column(Text)
    lava_currency: Mapped[str | None] = mapped_column(String(8))   # RUB | EUR | USD
    # Email для счёта Lava (обязателен в их API). Спрашиваем у человека.
    email: Mapped[str | None] = mapped_column(String(255))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Когда выдали промокод на бесплатный месяц клуба (только покупателям
    # полного интенсива). NULL — ещё не выдавали; защищает от повторной выдачи.
    club_promo_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Одноразовая инвайт-ссылка в чат участников — выдана этому человеку.
    invite_link: Mapped[str | None] = mapped_column(Text)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Заявка на интенсив из чек-листа: человек нажал «Собрать своего за 20 €»
    # и пришёл в бота (решение Николь 05.09.2026). Метка говорит, откуда он
    # пришёл (`cheklist`, `statya`, `post`), дата — когда заявился.
    # `created_at` для этого не годится: он значит «впервые у бота», а заявку
    # оставляет и тот, кто в боте уже год. Обе колонки nullable: у прежних
    # строк заявки не было, и это правда, а не пропуск.
    applied_source: Mapped[str | None] = mapped_column(String(16), index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Атрибуция агента (?start=ref_<slug>) — та же схема, что на /pay.
    ref_slug: Mapped[str | None] = mapped_column(String(16), index=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), index=True)
    # Прогрев: докуда дошла цепочка и когда касались последний раз.
    warmup_step: Mapped[int] = mapped_column(Integer, default=0)
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Человек попросил не писать — прогрев останавливается (уважаем отказ).
    unsubscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ChannelSubscriber(Base):
    """Человек у привратника закрытого канала Николь (план 2026-08-03).

    Канал про деньги и риски → вход только после самодекларации «мне есть 18».
    Строка живёт от первого вопроса до выданного доступа и хранит ГЛАВНОЕ —
    дату подтверждения возраста. Без неё «мы спрашивали» ничем не подтвердить.

    Почему отдельная таблица, а не поле в `IntensiveLead`: там путь клиента
    интенсива (счёт, оплата, чат участников). Смешать два потока в одной строке —
    значит однажды выдать доступ в канал за оплату интенсива или наоборот.

    Безопасность: `telegram_id`, username и имя — ПД, в лог уходит только id.
    Инвайт одноразовый и на 24 часа — общую ссылку в закрытый канал не раздаём.
    """
    __tablename__ = "channel_subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    # asked → confirmed (сказал «есть 18») → invited (ссылка выдана) →
    # in_channel (вошёл, подтверждено событием chat_member) → left (вышел).
    # declined — сказал «нет»; отказ не пожизненный, повторный заход снова спросит.
    status: Mapped[str] = mapped_column(String(16), default="asked", index=True)
    # Дата самодекларации 18+. Главное поле таблицы.
    age_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Откуда пришёл: deeplink (ссылка на бота) | join_request (заявка в канал).
    source: Mapped[str | None] = mapped_column(String(16))
    # Висит ли необработанная заявка на вступление: если да — доступ выдаётся
    # одобрением заявки, а не новой ссылкой.
    pending_request: Mapped[bool] = mapped_column(Boolean, default=False)
    invite_link: Mapped[str | None] = mapped_column(Text)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class BotSetting(Base):
    """Пара ключ→значение для настроек, которые бот узнаёт САМ в рантайме.

    Первый житель — `intensive_chat_id`: бота нельзя добавить в группу из кода
    (Telegram разрешает это только человеку), но когда его добавят, боту приходит
    событие `my_chat_member`. Бот ловит его и записывает chat_id сюда — Николь не
    нужно искать идентификатор чата и передавать его мне руками.
    """
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ClubMember(Base):
    """Участник платного клуба (план 2026-08-04).

    Клуб — отдельный продукт: своя подписка в Lava (`MONTHLY` / 90 / 180 дней),
    свой канал, свой учёт. Оплата интенсива доступа в клуб не даёт — поэтому
    таблица отдельная от `IntensiveLead`, а не пара полей в ней.

    ⚠️ ГЛАВНОЕ ПРАВИЛО ЭТОЙ ТАБЛИЦЫ: бот удаляет из канала ТОЛЬКО тех, кто здесь
    есть. Кто пришёл в канал до запуска платной модели (решение Николь 04.08.2026:
    «оставить бесплатно навсегда»), в таблицу не попадает — и удалить его цикл
    физически не может. Это сильнее любого флага: список на удаление собирается
    из строк таблицы, а не из состава канала.

    Безопасность: `telegram_id`, username, имя и email — ПД. В лог уходит только
    id, инвайт одноразовый и персональный.
    """
    __tablename__ = "club_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    # new → invoiced (счёт выставлен) → active (оплачено, в канале) →
    # expiring (идёт цепочка напоминаний) → asked (задан вопрос в день окончания) →
    # removed (удалён из канала). free — гость Николь, цепочка его не трогает.
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    # Счёт в Lava и период, на который он выставлен: MONTHLY | PERIOD_90_DAYS |
    # PERIOD_180_DAYS. По периоду считается, докуда оплачено.
    lava_invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    lava_invoice_url: Mapped[str | None] = mapped_column(Text)
    # Когда выставили счёт. По нему фоновая проверка отбирает СВЕЖИЕ счета:
    # брошенный счёт полугодовой давности иначе опрашивался бы у кассы каждую
    # минуту вечно.
    invoiced_at: Mapped[datetime | None] = mapped_column(DateTime)
    lava_currency: Mapped[str | None] = mapped_column(String(8))
    lava_periodicity: Mapped[str | None] = mapped_column(String(24))
    # Id подписки в Lava, если он появится в /api/v1/subscriptions: на первой
    # живой подписке формат ответа будет виден, до тех пор поле пустует.
    lava_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Счёт, который УЖЕ зачтён. Без этого поля один оплаченный счёт продлевал
    # доступ сколько угодно раз: кнопка «Проверить оплату» живёт в чате вечно,
    # и каждое нажатие добавляло бы новый период.
    paid_invoice_id: Mapped[str | None] = mapped_column(String(64))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Когда человеку дали отсрочку по кнопке «Оплачу». Одна на период: иначе
    # нажатие продлевает доступ на три дня бесконечно, без единой оплаты.
    grace_given_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Когда последний раз не удалось выдать инвайт (нет канала, нет прав).
    # Защищает от письма человеку и Николь каждую минуту.
    invite_failed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Докуда оплачено. Единственное поле, по которому строится вся цепочка.
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # Сколько раз человек уже платил. 0 → первый вход, ему показываем месяц;
    # ≥1 → в напоминаниях предлагаем ТОЛЬКО квартал и полгода (решение Николь).
    payments_count: Mapped[int] = mapped_column(Integer, default=0)
    invite_link: Mapped[str | None] = mapped_column(Text)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Цепочка удержания: докуда дошли (0 — не начиналась) и когда касались.
    # Шаг растёт только вперёд, поэтому повторный прогон не шлёт то же дважды.
    reminder_step: Mapped[int] = mapped_column(Integer, default=0)
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Когда человеку РЕАЛЬНО доставлено последнее письмо цепочки (вопрос в день
    # окончания). Отдельно от `reminder_step`: шаг растёт и при неудачной
    # отправке, а «предупреждён» должно значить «сообщение дошло». Иначе тот,
    # кто заблокировал бота, считался бы предупреждённым и вылетал бы на третий
    # день просрочки вместо четырнадцатого.
    warned_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Ответ на вопрос «что было полезным» — причина оттока и материал для контента.
    feedback: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Человек попросил не писать — цепочка останавливается (уважаем отказ).
    unsubscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
