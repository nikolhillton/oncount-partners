import asyncio
import hmac
import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    COOKIE_NAME,
    current_partner,
    decode_jwt,
    find_partner_by_phone,
    hash_login_code,
    issue_jwt,
    normalize_phone,
    verify_login_code,
)
from app.calc_config import calc_data
from app.config import settings
from app.email import send_magic_link
from app.wazzup import send_wa_code
from app.db import SessionLocal, engine, get_session
from app.models import (
    Base,
    Course,
    EmailLoginToken,
    EventRegistration,
    FaqItem,
    HealthAlert,
    Lead,
    LinkClick,
    LoginSession,
    MessageTemplate,
    PageView,
    Partner,
    PartnerIdentity,
    PaymentClaim,
    PhoneLoginToken,
    ProductBlock,
    QuizSubmission,
    Referral,
)
from app.refgen import generate_ref_slug
from app.seed import seed_if_empty
from app.usage import classify_path, flush_page_views, record_view
from app import linkstat

LOGIN_SESSION_TTL = timedelta(minutes=10)
# Магическая ссылка входа по email (план 2026-05-23).
EMAIL_TOKEN_TTL = timedelta(minutes=15)
EMAIL_RATE_LIMIT = 3  # запросов на один email за окно EMAIL_TOKEN_TTL
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Вход по номеру телефона — код в WhatsApp (план 2026-05-27).
PHONE_CODE_TTL = timedelta(minutes=10)
PHONE_CODE_MAX_ATTEMPTS = 5  # неверных вводов кода до блокировки токена
PHONE_RATE_LIMIT = 3         # запросов кода на один номер за окно PHONE_CODE_TTL
PHONE_MIN_DIGITS = 9         # короче — заведомо мусор, код не шлём

# Самостоятельная регистрация по WhatsApp (план 2026-08-07, решение Николь).
# Раньше код уходил только известному агенту; теперь неизвестный номер тоже может
# завести кабинет. Плата за это — мы соглашаемся отправить WhatsApp-сообщение по
# чужому желанию, а шлём с рабочего номера, который обслуживает клиентов. Поэтому
# два потолка: сколько кодов уходит незнакомцам за сутки со всей платформы и сколько
# запросов на НЕИЗВЕСТНЫЕ номера приходит с одного IP. Исчерпан любой из них —
# ведём себя как до фичи: кода нет, страница отвечает ровно то же самое.
WA_SELFREG_DAILY_LIMIT = 30      # кодов на неизвестные номера за сутки (вся платформа)
WA_SELFREG_IP_DAILY_LIMIT = 3    # запросов кода на неизвестный номер с одного IP за сутки
_wa_selfreg_ip_hits: dict[str, list[datetime]] = {}  # ip → времена запросов, живёт в процессе
_wa_selfreg_day: list[datetime] = []                 # времена всех выдач незнакомцам за сутки
# ⚠️ Считаем СВОИ выдачи в памяти, а не строки в БД. Первый вариант счётчика брал
# «партнёры без telegram_id, но с телефоном, созданные за сутки» — и ловил агентов,
# которых бэкфилл заводит из комиссионного Excel: чужие записи съедали бы лимит и
# глушили регистрацию на ровном месте. Отдельного поля-источника у Partner нет
# (`segment` занят квизом), заводить колонку ради счётчика — лишняя миграция.
# Плата за память: рестарт процесса обнуляет счётчик. Это осознанно — потолок нужен
# как аварийный тормоз против шквала в моменте, а не как бухгалтерия за месяц.

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# Стадии «пути клиента» по лиду — единый источник истины для кабинета.
# Цвет завязан на CSS-класс .status-<status> (static/css/oncount.css);
# здесь — только человекочитаемый ярлык (ru/en) + иконка. Под-статусы НЕ
# заводим (решение Николь): рисуем поверх 4 существующих Lead.status.
LEAD_STAGES: dict[str, dict[str, str]] = {
    "new":         {"icon": "📥", "ru": "Принят",   "en": "Received"},
    "in_progress": {"icon": "🧮", "ru": "В работе", "en": "In progress"},
    "won":         {"icon": "✅", "ru": "Оплачено", "en": "Paid"},
    "lost":        {"icon": "",   "ru": "Отказ",    "en": "Declined"},
}


def lead_stage(status: str, lang: str = "ru") -> dict[str, str]:
    """Стадия лида → {label, icon, status} для шаблонов кабинета.
    Неизвестный/пустой статус деградирует мягко: показываем сырое значение
    без иконки, чтобы шаблон никогда не падал."""
    lang = lang if lang in ("ru", "en") else "ru"
    stage = LEAD_STAGES.get(status or "")
    if not stage:
        return {"label": status or "—", "icon": "", "status": status or ""}
    return {"label": stage[lang], "icon": stage["icon"], "status": status}


# Статус партнёрского вознаграждения по ВЫИГРАННОМУ лиду — единый источник
# истины кабинета (Фаза B, план 2026-05-27). Деньги показываем ТОЛЬКО по won;
# под-таблицу выплат НЕ заводим (решение Николь) — значение в Lead.payout_state,
# менеджер ставит вручную (scripts/set_payout_state.py). hint — короткий смысл
# статуса для партнёра (репутация = операционное доверие).
PAYOUT_STATES: dict[str, dict[str, str]] = {
    "in_calc": {"icon": "🧾", "ru": "В расчёте", "en": "Being calculated",
                "hint_ru": "Считаем ваше вознаграждение по этому клиенту.",
                "hint_en": "We’re calculating your reward for this client."},
    "to_pay":  {"icon": "⏳", "ru": "К выплате", "en": "To be paid",
                "hint_ru": "Вознаграждение подтверждено, готовим выплату.",
                "hint_en": "Reward confirmed — payout is on the way."},
    "paid":    {"icon": "✅", "ru": "Выплачено", "en": "Paid",
                "hint_ru": "Вознаграждение по этому клиенту выплачено.",
                "hint_en": "Your reward for this client has been paid."},
}

# Разрешённые значения payout_state — единый список для валидации (UI и CLI).
PAYOUT_STATE_VALUES: tuple[str, ...] = tuple(PAYOUT_STATES.keys())

# Средняя комиссия партнёра с закрывшейся сделки (AED). Используется ТОЛЬКО как
# серый ориентир в столбце «Ваша комиссия» по ещё не закрытым лидам — не для
# расчёта реальных выплат (те лежат в Lead.commission_aed, проставляются вручную).
# Цифра от Николь 2026-07-21. Обязана быть подписана «в среднем по партнёрам»:
# у конкретного агента факт бывает ниже (у Dubru за сделку Павла — 550 AED), и
# без подписи среднее читается как обещание.
AVG_COMMISSION_AED: int = 1450


def payout_label(lead: Lead, lang: str = "ru") -> dict[str, str] | None:
    """Статус вознаграждения по лиду → {state, label, icon, hint} для шаблонов.
    Деньги показываем ТОЛЬКО по выигранным (won) лидам — по остальным рано,
    возвращаем None (шаблон не рисует ничего про деньги). У won без явного
    payout_state дефолт «в расчёте» выводим здесь (в данные не пишем).
    Неизвестное значение деградирует мягко: сырой текст без иконки/подсказки."""
    if getattr(lead, "status", None) != "won":
        return None
    lang = lang if lang in ("ru", "en") else "ru"
    state = lead.payout_state or "in_calc"
    meta = PAYOUT_STATES.get(state)
    if not meta:
        return {"state": state, "label": state, "icon": "", "hint": ""}
    return {"state": state, "label": meta[lang], "icon": meta["icon"], "hint": meta[f"hint_{lang}"]}


# Типы партнёра для раздела «Материалы / Партнёрский кит» (Фаза C, план
# 2026-05-27) — единый источник истины. Ключ = MessageTemplate.partner_type;
# партнёр сам выбирает свой тип вкладками (решение Николь). Ярлыки ru/en, как
# LEAD_STAGES. ВАЖНО (приватность): тип `insider` (скрытые рефереры, Imran)
# подписан нейтрально «Конфиденциально / Confidential» — нигде не светим «банк/
# инсайдер/комиссия» ([[feedback_brand_name_oncount]], перекличка с Фазой G).
# Порядок ключей = порядок вкладок.
PARTNER_TYPES: dict[str, dict[str, str]] = {
    "employee":   {"icon": "💼", "ru": "В найме / сотрудник",      "en": "Employed professional"},
    "solo":       {"icon": "🧑‍💻", "ru": "Соло-консультант",         "en": "Solo operator"},
    "events":     {"icon": "🎤", "ru": "События и сообщества",      "en": "Events & community"},
    "agency":     {"icon": "🏷️", "ru": "Агентство (white-label)",  "en": "Agency (white-label)"},
    "media":      {"icon": "📣", "ru": "Медиа и блог",             "en": "Media & blog"},
    "consultant": {"icon": "📊", "ru": "Консультант / фин-директор", "en": "Consultant / CFO"},
    "insider":    {"icon": "🔒", "ru": "Конфиденциально",          "en": "Confidential"},
}


def partner_type_label(key: str, lang: str = "ru") -> dict[str, str]:
    """Тип партнёра → {key, label, icon} для вкладок/заголовков /kits.
    Неизвестный/пустой ключ деградирует мягко: сырое значение без иконки."""
    lang = lang if lang in ("ru", "en") else "ru"
    meta = PARTNER_TYPES.get(key or "")
    if not meta:
        return {"key": key or "", "label": key or "—", "icon": ""}
    return {"key": key, "label": meta[lang], "icon": meta["icon"]}


# ─── Способы привлечения — ось вкладок /tools (план 2026-06-02) ──────────────
# Партнёр выбирает не «кто я» (тип), а «каким действием привожу» (способ).
# Порядок METHODS = порядок вкладок (Интро / Рассылка / Пост / Чек-лист).
# 2026-07-21 по решению Николь «События» перестали быть отдельной вкладкой:
# блок про совместные мероприятия живёт внутри «Поста» — там та же ситуация
# «у меня есть аудитория». hint = строка-фильтр «для кого» в шапке блока.
# Названия КОРОТКИЕ — чтобы 4 вкладки влезли в одну строку. Отдельного
# блока «прямые ссылки» нет: персональная ссылка уже вшита в каждый текст через
# {link}, дублировать список не нужно. EN-ярлыки тут же (ось внутренняя).
METHODS: dict[str, dict[str, str]] = {
    "intro":      {"icon": "💬", "ru": "Интро",     "en": "Intro",
                   "hint_ru": "тёплому клиенту 1-на-1",
                   "hint_en": "a warm 1-on-1 client"},
    "broadcast":  {"icon": "📨", "ru": "Рассылка",  "en": "Broadcast",
                   "hint_ru": "по своей базе контактов",
                   "hint_en": "to your contact base"},
    "social":     {"icon": "📱", "ru": "Пост",      "en": "Post",
                   "hint_ru": "пост или мероприятие",
                   "hint_en": "a post or an event"},
    "leadmagnet": {"icon": "📋", "ru": "Чек-лист",  "en": "Checklist",
                   "hint_ru": "подарить чек-лист",
                   "hint_en": "gift a checklist"},
}
METHODS_ORDER: list[str] = list(METHODS.keys())
# Какой способ раскрыт при заходе на дашборд (решение Николь 2026-07-23): «Пост»,
# а не первая вкладка — там лежит готовый креатив мастер-класса, с него партнёру
# и надо начинать. Порядок кнопок при этом не меняется. Если ключа нет в METHODS
# (переименовали способ) — фолбэк на первую вкладку, раздел не ломается.
METHODS_DEFAULT: str = "social" if "social" in METHODS else METHODS_ORDER[0]

# Старые якоря /tools → новые способы (bot.py и закладки не ломаем). directlinks
# больше нет отдельной вкладкой — ссылки переехали в `intro`, туда же #links.
LEGACY_TOOL_ANCHORS: dict[str, str] = {
    "links": "intro",
    "directlinks": "intro",
    "messages": "broadcast",
    "kits": "intro",
}

# Сертифицированные бухгалтеры — блок доверия в кружочках (решение Николь
# 2026-06-02): на квиз-лендинге /consultation и в кабинете у приглашения на
# консультацию. Только визуал доверия (без клика). Майя — реальное имя/роль
# (главбух, фото 391), Омер — ведущий бухгалтер (фото 394, с волосами и очками).
# 2026-07-21 (решение Николь) убран «Радж» — условное имя: под заявлением о
# квалификации ACCA вымышленный человек работает против доверия. Вернуть
# четвёртого — только реального, с фото.
# Языки — текстовыми кодами RU/EN/AR (флаг-эмодзи не рендерятся на Windows).
# Фото в static/img/accountants/.
# Всего бухгалтеров в отделе (решение Николь 2026-07-21). Фото есть не на всех,
# поэтому в блоке доверия после карточек стоит кружок «+N» с языками команды —
# честно показывает масштаб, не выдавая троих за весь отдел. Число здесь, а не
# в шаблоне (правило репо №1). Меняется вместе с составом отдела.
ACCOUNTANTS_TOTAL = 8
# Подпись к блоку бухгалтеров. В коде, а не в шаблоне: одна и та же строка нужна
# и в кабинете (под заголовком «Кто работает с вашим клиентом»), и на квиз-
# лендингах (внутри блока). Формулировка выверена с Николь 2026-07-21 —
# см. комментарий в _accountants.html о том, что было до неё.
ACCOUNTANTS_TITLE = {
    "ru": "Бухгалтеры с квалификацией ACCA. Отчётность по МСФО и правилам FTA ОАЭ",
    "en": "ACCA-qualified accountants. Reporting under IFRS and UAE FTA rules",
}
ACCOUNTANTS: list[dict] = [
    {"photo": "/static/img/accountants/maya.jpg", "name": "Майя Мандзюк",
     "name_en": "Maya Mandziuk", "role": "Главный бухгалтер",
     "role_en": "Chief accountant", "langs": ["ru", "gb"],
     "exp": "10+ лет опыта", "exp_en": "10+ yrs"},
    {"photo": "/static/img/accountants/omer.jpg", "name": "Омер",
     "name_en": "Omer", "role": "Ведущий бухгалтер",
     "role_en": "Lead accountant", "langs": ["gb", "ae"],
     "exp": "5+ лет опыта", "exp_en": "5+ yrs"},
    {"photo": "/static/img/accountants/lesia.jpg", "name": "Леся",
     "name_en": "Lesia", "role": "Ведущий бухгалтер", "role_en": "Lead accountant",
     "langs": ["ru", "gb"], "exp": "8+ лет опыта", "exp_en": "8+ yrs"},
]


def method_label(key: str, lang: str = "ru") -> dict[str, str]:
    """Способ → {key, label, icon, hint} для вкладок/шапки блока /tools.
    Неизвестный/пустой ключ деградирует мягко: сырое значение без иконки."""
    lang = lang if lang in ("ru", "en") else "ru"
    meta = METHODS.get(key or "")
    if not meta:
        return {"key": key or "", "label": key or "—", "icon": "", "hint": ""}
    return {
        "key": key,
        "label": meta[lang],
        "icon": meta["icon"],
        "hint": meta["hint_en" if lang == "en" else "hint_ru"],
    }


def _personal_links(ref: str, base: str, lang: str = "ru") -> dict[str, str]:
    """Все персональные ссылки партнёра по ключам link_key. Один источник истины
    для вкладок /tools и для подстановки плейсхолдера {link} в тело текста.
    Квизы /consultation и /mk — наш домен, ?ref= метит лида нативно; TG/WA —
    редиректы /ct,/cw,/mt,/mw; partner_bot — приглашение нового партнёра.
    lang='en' → квиз-ссылки получают &lang=en: EN-тексты агента ведут клиента
    на английскую версию лендинга (план 2026-07-21, пункт 20 аудита)."""
    q = "&lang=en" if lang == "en" else ""
    return {
        "consult_quiz": f"{base}/consultation?ref={ref}{q}",
        "consult_tg":   f"{base}/ct/{ref}",
        "consult_wa":   f"{base}/cw/{ref}",
        "mk_quiz":      f"{base}/mk?ref={ref}{q}",
        "mk_tg":        f"{base}/mt/{ref}",
        "mk_wa":        f"{base}/mw/{ref}",
        # Лид-магниты: квиз → PDF чек-листа ссылкой в WhatsApp (?ref метит лида).
        # Ключи ≤16 символов — ограничение колонки MessageTemplate.link_key VARCHAR(16).
        "lm_corptax":   f"{base}/guide/corp-tax?ref={ref}{q}",
        "lm_5mistakes": f"{base}/guide/5-mistakes?ref={ref}{q}",
        # Чек-лист про AI-сотрудника: заведён 04.09.2026 в реестре тем
        # (leadmagnet_topics, ключ ai-sotrudnik). Партнёру нужен свой ?ref,
        # иначе приведённые им люди уйдут в воронку без его метки.
        "lm_aisotrud":  f"{base}/guide/ai-sotrudnik?ref={ref}{q}",
        "partner_bot":  f"{base}/p/{ref}",
    }


# ─── Анкета партнёра (Фаза L, план 2026-05-27) ──────────────────────────────
# ⚠️ ТЕКСТЫ ВОПРОСОВ/ВАРИАНТОВ — ЧЕРНОВИК на утверждении Николь (НЕ выдаём за
# финальные, урок Фаз E/F). Правятся ЗДЕСЬ без миграции — ответы лежат в JSON
# по ключу варианта, а не по тексту. Снять флаг SURVEY_DRAFT после утверждения.
#
# Структура решена Николь 2026-06-01:
#   • список «сфера» — из плана Фазы L (НЕ переиспользуем SEGMENTS/PARTNER_TYPES);
#   • «опыт» — по рынку ОАЭ; «поток B2B» — да/нет + диапазон; «соцсети» —
#     каналы (мультивыбор) + ориентир аудитории;
#   • «выплаты» — ТОЛЬКО ТИП канала (белый список). Номера карт/кошельков/IBAN
#     в БД НЕ пишем — критерий безопасности (ПД, «опасная тройка»).
SURVEY_DRAFT = False  # тексты утверждены Николь 2026-06-02 (Фаза 4 go-live)

# Каждый вариант: (value, ru, en). value — стабильный ключ в JSON-ответах.
SURVEY_OPTIONS: dict[str, list[tuple[str, str, str]]] = {
    "sphere": [
        ("consulting",        "Консалтинг",                   "Consulting"),
        ("bank_accounts",     "Открытие банковских счетов",   "Bank account opening"),
        ("real_estate",       "Недвижимость",                 "Real estate"),
        ("company_formation", "Регистрация компаний",         "Company formation"),
        ("golden_visa",       "Golden Visa / визы",           "Golden Visa / visas"),
        ("finance_insurance", "Финансы и страхование",        "Finance & insurance"),
        ("marketing_pr",      "Маркетинг и PR",               "Marketing & PR"),
        ("events",            "События и сообщества",         "Events & community"),
        ("media_influencer",  "Медиа / инфлюенсер",           "Media / influencer"),
        ("other",             "Другое",                       "Other"),
    ],
    "uae_experience": [
        ("lt1", "До 1 года",   "Under 1 year"),
        ("lt3", "До 3 лет",    "Under 3 years"),
        ("lt5", "До 5 лет",    "Under 5 years"),
        ("gt5", "Свыше 5 лет", "Over 5 years"),
    ],
    "b2b_flow": [
        ("steady",     "Да, постоянно",      "Yes, steady"),
        ("occasional", "Время от времени",   "From time to time"),
        ("none",       "Пока нет",           "Not yet"),
    ],
    "b2b_volume": [
        ("1-5",    "1–5 в месяц",    "1–5 a month"),
        ("5-20",   "5–20 в месяц",   "5–20 a month"),
        ("20-50",  "20–50 в месяц",  "20–50 a month"),
        ("50plus", "50+ в месяц",    "50+ a month"),
    ],
    "base_size": [
        ("lt50",     "До 50",     "Under 50"),
        ("50-200",   "50–200",    "50–200"),
        ("200-1000", "200–1000",  "200–1000"),
        ("1000plus", "1000+",     "1000+"),
    ],
    "social_channels": [
        ("instagram", "Instagram",       "Instagram"),
        ("telegram",  "Telegram",        "Telegram"),
        ("linkedin",  "LinkedIn",        "LinkedIn"),
        ("youtube",   "YouTube",         "YouTube"),
        ("tiktok",    "TikTok",          "TikTok"),
        ("facebook",  "Facebook",        "Facebook"),
        ("none",      "Нет соцсетей",    "No social channels"),
        ("other",     "Другое",          "Other"),
    ],
    "social_audience": [
        ("lt1k",    "До 1 000",   "Under 1k"),
        ("1-10k",   "1–10 тыс.",  "1–10k"),
        ("10-50k",  "10–50 тыс.", "10–50k"),
        ("50kplus", "50 тыс.+",   "50k+"),
    ],
    "payout_method": [
        ("card",   "Банковская карта",       "Bank card"),
        ("bank",   "Банковский счёт (IBAN)", "Bank account (IBAN)"),
        ("crypto", "Криптовалюта (USDT)",    "Crypto (USDT)"),
    ],
}

# Заголовок вопроса (ru, en). Порядок отображения и для менеджер-сводки —
# SURVEY_FIELD_ORDER ниже.
SURVEY_LABELS: dict[str, tuple[str, str]] = {
    "sphere":          ("В какой сфере вы работаете?",
                        "What's your field?"),
    "uae_experience":  ("Как давно вы в B2B-сфере?",
                        "How long have you been in B2B?"),
    "b2b_flow":        ("Есть ли у вас поток клиентов-предпринимателей?",
                        "Do you have a flow of business clients?"),
    "b2b_volume":      ("Сколько примерно клиентов в месяц?",
                        "Roughly how many clients a month?"),
    "base_size":       ("Насколько большая у вас база контактов?",
                        "How large is your contact base?"),
    "social_channels": ("У вас есть соцсети, в которых можно продвигать услугу бухгалтерии?",
                        "Do you have social media where you could promote accounting services?"),
    "social_audience": ("Ориентир по размеру аудитории",
                        "Approximate audience size"),
    "payout_method":   ("Как удобнее получать партнёрское вознаграждение?",
                        "How would you prefer to receive partner rewards?"),
}

# Порядок вопросов в форме и в сводке для менеджера.
SURVEY_FIELD_ORDER = [
    "sphere", "uae_experience", "b2b_flow", "b2b_volume",
    "base_size", "social_channels", "social_audience", "payout_method",
]
# Поля, обязательные на сервере (остальные — условные/опциональные).
SURVEY_REQUIRED = {"sphere", "uae_experience", "b2b_flow", "base_size", "payout_method"}
# Свободный текст к варианту «other» — длину режем, ПД не предполагается.
SURVEY_OTHER_MAXLEN = 120


def _survey_values(field: str) -> set[str]:
    """Белый список value по полю анкеты."""
    return {o[0] for o in SURVEY_OPTIONS.get(field, [])}


def partner_onboarding(partner: Partner, lang: str = "ru") -> dict:
    """Единый источник по анкете партнёра (Фаза L): статус + человекочитаемые
    ответы. Используется баннером (`completed`), GET-формой (`answers` для
    предзаполнения) и админ-просмотром менеджера (`summary`).
    Мягко деградирует: пустые/неизвестные значения не валят рендер."""
    lang = lang if lang in ("ru", "en") else "ru"
    li = 2 if lang == "en" else 1  # индекс ru/en в кортеже варианта
    answers: dict = partner.onboarding_answers or {}
    completed = partner.survey_completed_at is not None

    def label(field: str, val: str) -> str:
        for o in SURVEY_OPTIONS.get(field, []):
            if o[0] == val:
                return o[li]
        return val  # текст «other» или неизвестный ключ — как есть

    summary: list[dict] = []
    for field in SURVEY_FIELD_ORDER:
        raw = answers.get(field)
        if not raw:
            continue
        if isinstance(raw, list):
            value_label = ", ".join(label(field, v) for v in raw)
        else:
            value_label = label(field, raw)
        # «other»-текст хранится отдельным ключом <field>_other.
        extra = answers.get(f"{field}_other")
        if extra:
            value_label = f"{value_label}: {extra}"
        summary.append({
            "field": field,
            "question": SURVEY_LABELS.get(field, ("", ""))[0 if lang == "ru" else 1],
            "value": value_label,
        })
    return {"completed": completed, "answers": answers, "summary": summary}


# Партнёрский менеджер — ОДИН общий на всех партнёров (решение Николь 2026-05-28,
# Фаза E). Единый источник истины кабинета: имя/контакт/SLA в одном месте, БЕЗ
# поля в Partner и БЕЗ миграции. ВАЖНО (ПД, «опасная тройка»): имя и контакт —
# РЕАЛЬНЫЕ данные, их НЕ выдумываем. До подтверждения Николь стоят помеченные
# плейсхолдеры (name_confirmed / contact_confirmed = False) — UI тогда НЕ выдаёт
# их за живой контакт и не строит кликабельную ссылку. Фото — реальное из
# team-2026-05 (static/img/manager.jpg). SLA-формулировка совпадает с уже
# обещанной в transfer.html / seed.py / messages_text.py («в рабочее время в
# течение часа») — не противоречит существующей копии кабинета.
PARTNER_MANAGER: dict = {
    "photo": "/static/img/manager.jpg",  # реальное фото Николь (team-2026-05)
    # Подтверждено Николь 2026-05-28: менеджер партнёров = Николь Хилтон.
    "name_confirmed": True,
    # Латиницей — Nikole Hillton (НЕ Nicole/Hilton): так во всех её каналах.
    "name": {"ru": "Николь Хилтон", "en": "Nikole Hillton"},
    "role": {"ru": "Ваш партнёрский менеджер", "en": "Your partner manager"},
    # Контакты менеджера. channel ∈ {"whatsapp","telegram","email"};
    # value — цифры номера / username / email. confirmed=True → строим
    # кликабельную ссылку; False → UI помечает «на утверждении Николь» и НЕ
    # выдаёт выдуманные ПД за живой контакт. ВАЖНО (ПД): value НЕ выдумываем.
    "contacts": [
        # Номер WhatsApp подтверждён Николь 2026-05-28 (оканчивается на 14).
        {"channel": "whatsapp", "value": "971528553814", "confirmed": True},
        # Telegram-username из конфига проекта (CONTACT_TG_USERNAME), подтверждён Николь.
        {"channel": "telegram", "value": "nikol_hillton", "confirmed": True},
    ],
    # SLA — согласовано с уже обещанным в кабинете (transfer/seed/bot): час в
    # рабочее время. Подтверждено Николь 2026-05-28.
    "sla": {
        "ru": "Отвечаем по вашему клиенту в течение часа в рабочее время.",
        "en": "We reply about your client within an hour during business hours.",
    },
}


def partner_manager(lang: str = "ru") -> dict:
    """Данные партнёрского менеджера для шаблонов (единый источник, Фаза E).
    Возвращает локализованные строки, список готовых кликабельных контактов
    (links — только confirmed) и список каналов на утверждении (pending_channels),
    чтобы UI пометил их «на утверждении Николь» и не выдавал выдуманные ПД за
    живой контакт. Мягко деградирует на неизвестный канал (пропускаем)."""
    lang = lang if lang in ("ru", "en") else "ru"
    m = PARTNER_MANAGER
    links: list[dict] = []
    pending_channels: list[str] = []
    for c in m["contacts"]:
        ch = c.get("channel")
        if c.get("confirmed") and c.get("value"):
            v = str(c["value"]).strip()
            if ch == "whatsapp":
                digits = v.lstrip("+")
                links.append({"channel": ch, "href": f"https://wa.me/{digits}", "display": f"+{digits}"})
            elif ch == "telegram":
                uname = v.lstrip("@")
                links.append({"channel": ch, "href": f"https://t.me/{uname}", "display": f"@{uname}"})
            elif ch == "email":
                links.append({"channel": ch, "href": f"mailto:{v}", "display": v})
            # неизвестный канал — пропускаем (мягкая деградация)
        elif ch in ("whatsapp", "telegram", "email"):
            pending_channels.append(ch)
    return {
        "photo": m["photo"],
        "name": m["name"][lang],
        "name_pending": not m["name_confirmed"],
        "role": m["role"][lang],
        "sla": m["sla"][lang],
        "links": links,
        "pending_channels": pending_channels,
    }


# Доступно во всех шаблонах кабинета (DRY): _leads_table.html, dashboard.html.
templates.env.globals["lead_stage"] = lead_stage
templates.env.globals["payout_label"] = payout_label
templates.env.globals["partner_type_label"] = partner_type_label
templates.env.globals["method_label"] = method_label
templates.env.globals["partner_manager"] = partner_manager
# Дата выплаты по won-лиду (Фаза K) — единый источник для _leads_table.html.
from app.notifications import payout_due_date as _payout_due_date  # noqa: E402
templates.env.globals["payout_due_date"] = _payout_due_date
# Контакты для футера — из конфига (правило репо №1: не хардкодить ссылки).
# Тот же источник, что и короткие ссылки /ct /cw (settings.CONTACT_*).
templates.env.globals["contact_tg"] = settings.CONTACT_TG_USERNAME
templates.env.globals["contact_wa"] = settings.CONTACT_WA_NUMBER
# Telegram-id админа (Николь) — чтобы шаблон показывал пункт меню «Аналитика»
# только ей (раздел /admin/*). Гейт всё равно на сервере (require_admin).
templates.env.globals["admin_tg_id"] = settings.ADMIN_TG_ID
# Размер бухгалтерского отдела — для кружка «+N» в блоке доверия. Глобал, потому
# что блок _accountants.html подключается и в кабинете, и на квиз-лендингах.
templates.env.globals["accountants_total"] = ACCOUNTANTS_TOTAL
templates.env.globals["accountants_title"] = ACCOUNTANTS_TITLE
def fmt_amount(value, lang: str = "ru") -> str:
    """Сумма с разделителем тысяч под язык интерфейса: 14 700 / 14,700.

    Без него в кабинете соседствовали «14700 AED» и «$2,400» — англоязычный
    партнёр читает число без разделителя как опечатку. Неразрывный пробел в RU,
    чтобы разряд не переносился на новую строку.
    """
    try:
        n = int(round(float(value or 0)))
    except (TypeError, ValueError):
        return "0"
    s = f"{n:,}"
    return s if lang == "en" else s.replace(",", " ")
templates.env.globals["fmt_amount"] = fmt_amount
# Версия статики в ссылке на CSS (?v=…). Без неё браузер партнёра держит старый
# oncount.css после деплоя и показывает новую разметку со СТАРЫМИ стилями —
# ровно это поймали 2026-07-21 на блоке «Тексты и ссылки». Меняется вместе с
# файлом, т.е. только когда стили реально правились.
try:
    _CSS_MTIME = int((BASE_DIR / "static" / "css" / "oncount.css").stat().st_mtime)
except OSError:  # файла нет (тесты/битый образ) — версия не критична
    _CSS_MTIME = 0
templates.env.globals["static_v"] = str(_CSS_MTIME)

app = FastAPI(title="ONCOUNT Partner Platform")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ─── Rate-limit чувствительных роутов (security-review 2026-05-26) ───────────
# In-memory sliding window per IP. Ключей в словаре ограниченное число: старые
# бакеты подчищаются свипом раз в окно + жёсткий потолок _RL_MAX_KEYS — иначе
# _RL_HITS рос бы на каждый новый IP без предела (medium-DoS по памяти).
_RL_HITS: dict[str, deque] = {}
from app.leadmagnet_topics import submit_rl_paths as _lm_submit_rl_paths  # noqa: E402

_RL_PATHS = ("/auth/", "/login", "/invite/", "/consultation/submit", "/mk/submit",
             "/guide/corp-tax/submit",
             "/guide/5-mistakes/submit", "/pay/confirm") + _lm_submit_rl_paths()
_RL_MAX = 30            # запросов с одного IP
_RL_WINDOW = 60         # за столько секунд
_RL_MAX_KEYS = 50_000   # потолок отслеживаемых IP (предохранитель памяти)
_RL_SWEEP_AT = [0.0]    # время последнего свипа (list — мутируем из middleware)


def _client_ip(request: Request) -> str:
    """Клиентский IP за прокси Railway. Берём ПЕРВЫЙ (левый) IP из X-Forwarded-For:
    по документации Railway их edge затирает клиентский XFF и ставит реальный IP
    слева, так что на прямом railway-домене подделать его нельзя. Правый IP брать
    НЕЛЬЗЯ — там внутренний адрес балансировщика, один на всех: все пользователи
    схлопнулись бы в один бакет и легли бы вместе. Фоллбэк — прямой peer-адрес."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "?"


def _rl_sweep(now: float) -> None:
    """Убрать протухшие/пустые бакеты, чтобы _RL_HITS не рос без предела."""
    dead = [ip for ip, dq in _RL_HITS.items() if not dq or now - dq[-1] > _RL_WINDOW]
    for ip in dead:
        _RL_HITS.pop(ip, None)


def _rl_register(ip: str, now: float) -> bool:
    """Учесть запрос с ip в момент now. True — в пределах лимита (пропускаем),
    False — лимит превышен (429). Подрезает протухшие метки; при достижении
    потолка ключей новый IP не заводит (fail-open: лимит по IP — мягкий слой,
    реальную защиту от перебора держат лимиты по номеру/токену в БД)."""
    dq = _RL_HITS.get(ip)
    if dq is None:
        if len(_RL_HITS) >= _RL_MAX_KEYS:
            return True
        dq = _RL_HITS[ip] = deque()
    while dq and now - dq[0] > _RL_WINDOW:
        dq.popleft()
    if len(dq) >= _RL_MAX:
        return False
    dq.append(now)
    return True


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Per-IP sliding-window лимит на чувствительные роуты (вход/инвайт/заявки) —
    против брутфорса и енумерации (security-review 2026-05-26)."""
    path = request.url.path
    if any(path.startswith(p) for p in _RL_PATHS):
        now = time.time()
        if now - _RL_SWEEP_AT[0] > _RL_WINDOW:
            _RL_SWEEP_AT[0] = now
            _rl_sweep(now)
        if not _rl_register(_client_ip(request), now):
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("Too many requests", status_code=429)
    return await call_next(request)


@app.middleware("http")
async def persist_lang_cookie(request: Request, call_next):
    """Делает выбор языка «липким»: ?lang=en|ru → кука lang на год. Без этого язык
    терялся при первом же переходе по ссылке (ссылки не таскают ?lang)."""
    response = await call_next(request)
    q = request.query_params.get("lang")
    if q in ("en", "ru"):
        response.set_cookie("lang", q, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response


@app.middleware("http")
async def track_page_view(request: Request, call_next):
    """Трекинг использования кабинета (план 2026-06-03). Пишем заход залогиненного
    агента на известную страницу кабинета: partner_id + нормализованный путь +
    время. Белый список путей (classify_path), БЕЗ query-строки (там бывают
    токены), без анонимов/лендингов/админки. partner_id берём из JWT cookie
    (decode_jwt, без запроса в БД). Только GET+200 → не-залогиненный получает
    редирект на /login (307) и не трекается. Запись идёт в буфер (app.usage),
    реальный commit делает фоновый джоб. Трекинг НИКОГДА не ломает ответ агенту."""
    response = await call_next(request)
    try:
        if request.method == "GET" and response.status_code == 200:
            hit = classify_path(request.url.path)
            if hit:
                token = request.cookies.get(COOKIE_NAME)
                pid = decode_jwt(token) if token else None
                if pid:
                    record_view(pid, hit[0], hit[1])
    except Exception:
        pass
    return response


log = logging.getLogger("oncount.startup")

# httpx на уровне INFO печатает полный URL каждого запроса — для Telegram это
# URL вида /bot<ТОКЕН>/sendMessage, т.е. токен бота утекает в логи Railway
# (находка 2026-07-21). Глушим до WARNING: ошибки видны, URL успешных — нет.
logging.getLogger("httpx").setLevel(logging.WARNING)


@app.on_event("startup")
async def on_startup() -> None:
    Base.metadata.create_all(engine)
    # One-off DDL: расширяем price_aed с VARCHAR(64) до TEXT — туда теперь идёт HTML.
    # Идемпотентно: если колонка уже TEXT, ALTER пройдёт без эффекта.
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE product_blocks ALTER COLUMN price_aed TYPE TEXT"))
        # Онбординг-поля партнёра — добавляются при первом деплое после изменения модели.
        for col in (
            "onboarded_at",
            "links_viewed_at",
            "products_viewed_at",
            "courses_viewed_at",
            "checklist_dismissed_at",
        ):
            conn.execute(text(f"ALTER TABLE partners ADD COLUMN IF NOT EXISTS {col} TIMESTAMP"))
        # Язык интерфейса бота (план 2026-05-23). Идемпотентно.
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS lang VARCHAR(2)"))
        # Фаза 0.7 (план 2026-05-26): связь Partner ↔ Kommo-агент + ref_slug инвайта.
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS kommo_agent_enum_id BIGINT"))
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS kommo_agent_name VARCHAR(128)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_partners_kommo_agent_enum_id ON partners (kommo_agent_enum_id)"))
        conn.execute(text("ALTER TABLE login_sessions ADD COLUMN IF NOT EXISTS ref_slug VARCHAR(16)"))
        conn.execute(text("ALTER TABLE email_login_tokens ADD COLUMN IF NOT EXISTS ref_slug VARCHAR(16)"))
        # EN-колонки контент-таблиц (план 2026-05-22). create_all не делает ALTER,
        # а таблицы уже существуют в проде — добавляем идемпотентно.
        en_cols = {
            "product_blocks": ("title_en", "price_aed_en", "summary_md_en", "full_md_en"),
            "message_templates": ("segment_en", "title_en", "body_md_en"),
            "faq_items": ("category_en", "question_en", "answer_md_en"),
        }
        for tbl, cols in en_cols.items():
            for col in cols:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} TEXT"))
        # Креатив к тексту (2026-07-23): картинка поста + её превью. create_all не
        # делает ALTER, а message_templates уже есть в проде. Идемпотентно.
        for col in ("image_path", "image_thumb"):
            conn.execute(
                text(f"ALTER TABLE message_templates ADD COLUMN IF NOT EXISTS {col} VARCHAR(255)")
            )
        # Вход по email (план 2026-05-23). Идемпотентно:
        # 1) telegram_id больше не обязателен — email-партнёр может быть без TG.
        conn.execute(text("ALTER TABLE partners ALTER COLUMN telegram_id DROP NOT NULL"))
        # 2) Уникальность email регистронезависимо, только для непустых значений
        #    (частичный индекс). create_all не создаёт частичных/выражательных индексов.
        #    ПРЕД-УСЛОВИЕ: на проде не должно быть дублей lower(email) — иначе упадёт;
        #    проверять перед первым деплоем (см. план, Фаза 1, пред-шаг).
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_partners_email_lower "
            "ON partners (lower(email)) WHERE email IS NOT NULL"
        ))
        # 3) Гигиена: чистим протухшие невостребованные email-токены (старше суток).
        conn.execute(text(
            "DELETE FROM email_login_tokens "
            "WHERE consumed_at IS NULL AND created_at < now() - interval '1 day'"
        ))
        # Вход по номеру телефона (план 2026-05-27). Таблицу создаёт create_all;
        # здесь только гигиена — чистим протухшие коды (старше суток), чтобы не
        # копить хэши и телефоны. Идемпотентно: нет таблицы на самом первом запуске
        # быть не может (create_all уже отработал выше).
        conn.execute(text(
            "DELETE FROM phone_login_tokens "
            "WHERE created_at < now() - interval '1 day'"
        ))
        # Статус вознаграждения по выигранному лиду (Фаза B, план 2026-05-27).
        # Аддитивно и идемпотентно: одна nullable-колонка, без DB-default —
        # дефолт «в расчёте» выводит payout_label. Существующие строки/колонки
        # не трогаются. create_all не делает ALTER, а leads уже есть в проде.
        # Бот оплат: что купили — первый день или весь интенсив (04.08.2026).
        conn.execute(text("ALTER TABLE intensive_leads ADD COLUMN IF NOT EXISTS product_code VARCHAR(16)"))
        conn.execute(text("ALTER TABLE intensive_leads ADD COLUMN IF NOT EXISTS club_promo_sent_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS payout_state VARCHAR(16)"))
        # Сумма комиссии партнёра по сделке (решение Николь 2026-07-21). Аддитивно
        # и идемпотентно: nullable, без DB-default — NULL значит «не посчитана»
        # и даёт 0 в «Заработано». Существующие строки не трогаются.
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS commission_aed NUMERIC(12,2)"))
        # «Что НЕ предлагать клиенту» — обязательное поле формы /transfer (Фаза F,
        # план 2026-05-27): защищает репутацию партнёра. Аддитивно, идемпотентно,
        # nullable: старые лиды и лиды из kommo_sync/бота — без этого поля.
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS do_not_offer TEXT"))
        # Якорь выплаты + идемпотентность win-пуша (Фаза K, план 2026-05-27).
        # Аддитивно и идемпотентно: две nullable-колонки без DB-default. won_at —
        # стабильный момент перехода в won (дата выплаты), won_notified_at — что
        # пуш уже обработан. create_all не делает ALTER, а leads уже есть в проде.
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS won_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS won_notified_at TIMESTAMP"))
        # Модуль выплат (план 2026-06-02, замена Excel). Аддитивно и идемпотентно:
        # nullable-колонки без DB-default (кроме payout_urgent). create_all не делает
        # ALTER, а leads уже есть в проде. Заполняет менеджер на /admin/payouts.
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS fee_aed NUMERIC(12,2)"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS payout_urgent BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS agreement_url TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS bank_details TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS payout_receipt_url TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS payout_paid_on VARCHAR(32)"))
        # Тип партнёра у шаблона-материала (Фаза C, план 2026-05-27). Аддитивно и
        # идемпотентно: одна nullable-колонка + индекс. NULL = генерик /messages
        # (старые строки не трогаются). create_all не делает ALTER, а
        # message_templates уже есть в проде.
        conn.execute(text("ALTER TABLE message_templates ADD COLUMN IF NOT EXISTS partner_type VARCHAR(32)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_templates_partner_type ON message_templates (partner_type)"))
        # Способ привлечения + ключ персональной ссылки (план 2026-06-02
        # «переборка /tools по способам»). Аддитивно и идемпотентно: две
        # nullable-колонки + индекс по method. NULL = текст не в новых вкладках.
        # create_all не делает ALTER, а message_templates уже есть в проде.
        conn.execute(text("ALTER TABLE message_templates ADD COLUMN IF NOT EXISTS method VARCHAR(32)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_message_templates_method ON message_templates (method)"))
        conn.execute(text("ALTER TABLE message_templates ADD COLUMN IF NOT EXISTS link_key VARCHAR(16)"))
        # Анкета партнёра (Фаза L, план 2026-05-27). Аддитивно и идемпотентно:
        # две nullable-колонки, без DB-default. onboarding_answers (JSON) — ответы
        # белого списка; survey_completed_at — отметка прохождения (NULL = не
        # пройдена → баннер показан). Существующие партнёры остаются без значений.
        # Тип JSON совпадает с моделью (как Referral.visitor_meta) — нет
        # расхождения dev/prod. ПД: номера карт/кошельков в JSON НЕ пишем.
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS onboarding_answers JSON"))
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS survey_completed_at TIMESTAMP"))
        # Доход 2-го уровня (решение Николь 2026-07-21). Аддитивно, идемпотентно,
        # nullable — NULL значит 0. Ручной ввод из комиссионного Excel.
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS l2_income_aed NUMERIC(12,2)"))
        # Разбивка 2-го уровня по суб-агентам (решение Николь 2026-07-21).
        # Аддитивно, идемпотентно, nullable JSON. NULL = показываем старое число.
        conn.execute(text("ALTER TABLE partners ADD COLUMN IF NOT EXISTS l2_income JSON"))
        # Дискриминатор лендинга у заявок квиза (план 2026-06-02): отделяет
        # регистрации мастер-класса от заявок /consultation. Аддитивно и
        # идемпотентно: одна nullable-колонка + индекс. NULL = /consultation
        # (старые строки не трогаются). create_all создаёт колонку на чистой БД,
        # ALTER — на случай уже существующей таблицы quiz_submissions.
        conn.execute(text("ALTER TABLE quiz_submissions ADD COLUMN IF NOT EXISTS event_slug VARCHAR(64)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_quiz_submissions_event_slug ON quiz_submissions (event_slug)"))
    with SessionLocal() as session:
        seed_if_empty(session)

    # Run the Telegram bot as an asyncio task in the same process as uvicorn.
    # Free Railway plan caps the number of services, so we co-locate web + bot.
    if settings.BOT_TOKEN:
        from app.bot import main as bot_main  # local import to avoid circular issues
        log.info("Launching bot polling as background task")
        asyncio.create_task(bot_main())
    else:
        log.info("BOT_TOKEN empty -> bot polling skipped, web only")

    # Бот оплат интенсива (@Nikol_hilton_bot, план 2026-08-03). Свой токен →
    # свой getUpdates, с партнёрским ботом не конфликтует. Пустой токен →
    # не поднимается, остальная платформа работает как раньше.
    if settings.PAY_BOT_TOKEN:
        from app.paybot import main as paybot_main
        log.info("Launching paybot polling as background task")
        asyncio.create_task(paybot_main())
    else:
        log.info("PAY_BOT_TOKEN empty -> paybot skipped")

    # APScheduler (отдельный поток). ВАЖНО (план 2026-07-23): планировщик стартует
    # БЕЗУСЛОВНО, чтобы мониторинг ссылок работал даже без ONCOUNT_API_URL; синк лидов
    # и digest остаются под своим гейтом.
    from datetime import datetime as _dt
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="UTC")
    # Мониторинг здоровья ссылок (план 2026-07-23) каждые 6ч. Считает всегда (лог +
    # /admin/analytics), в TG шлёт только при LINK_HEALTH_ALERTS / LINK_HEALTH_STATS_ALERTS
    # (оба default off — сперва dry, потом калибровка).
    from app.health import health_check_job
    sched.add_job(health_check_job, "cron", hour="*/6", id="link_health",
                  max_instances=1, coalesce=True)
    # Сброс буфера заходов кабинета (трекинг использования, план 2026-06-03) раз в 30с.
    # Заходы копятся в app.usage, тут пишутся пачкой — кабинету не блокируем ответ.
    # Вне гейта API: аналитика поведения не должна зависеть от наличия синка лидов.
    sched.add_job(flush_page_views, "interval", seconds=30,
                  id="flush_page_views", max_instances=1, coalesce=True)
    # Периодический синк лидов агентов (Фаза 1/кабинет, план 2026-05-26) + недельный
    # digest (Фаза K, реально шлёт только при NOTIFICATIONS_LIVE) — под гейтом API.
    if settings.ONCOUNT_API_URL:
        from app.kommo_sync import sync_agent_leads
        sched.add_job(sync_agent_leads, "interval", minutes=60, id="kommo_sync",
                      next_run_time=_dt.utcnow(), max_instances=1, coalesce=True)
        from app.notifications import digest_job
        sched.add_job(digest_job, "cron", hour=12, minute=0,
                      id="weekly_digest", max_instances=1, coalesce=True)
    sched.start()
    app.state.scheduler = sched
    log.info("scheduler started: link_health 6h + kommo_sync/digest=%s"
             " (notifications_live=%s, link_health_alerts=%s)",
             bool(settings.ONCOUNT_API_URL), settings.NOTIFICATIONS_LIVE, settings.LINK_HEALTH_ALERTS)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    # Дослать остаток буфера заходов, чтобы не потерять последние секунды при
    # штатной остановке (трекинг использования, план 2026-06-03).
    try:
        flush_page_views()
    except Exception:
        pass


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

# Разовые admin-эндпоинты seed/stats (Фаза 0.7) удалены после использования
# (security-review 2026-05-26: ключ=JWT_SECRET в query — риск утечки). seed/синк
# теперь только через CLI scripts/seed_agent_partners.py + APScheduler-синк.


@app.get("/debug/event-stats")
def debug_event_stats(request: Request, session: Session = Depends(get_session)) -> dict:
    # Раньше эндпоинт был публичным (агрегаты регистраций наружу без причины).
    # Закрываем под тот же админ-гейт, что и /admin/*: доступ из залогиненного
    # браузера Николь (её Telegram-cookie), аноним/чужой → 404 (не раскрываем).
    require_admin(request, session)
    from sqlalchemy import func
    rows = (
        session.query(EventRegistration.event_slug, func.count(EventRegistration.id))
        .group_by(EventRegistration.event_slug)
        .all()
    )
    by_event = {slug: count for slug, count in rows}
    total = sum(by_event.values())
    from_lending = (
        session.query(func.count(EventRegistration.id))
        .filter(EventRegistration.meta["source"].as_string() == "lending")
        .scalar()
    )
    return {"total": total, "by_event": by_event, "from_lending": from_lending}


def _lang(request: Request) -> str:
    # Выбор языка интерфейса: ?lang= имеет приоритет, иначе кука lang
    # (её ставит persist_lang_cookie), иначе русский по умолчанию.
    lang_raw = request.query_params.get("lang") or request.cookies.get("lang")
    return "en" if lang_raw == "en" else "ru"


def _ctx(request: Request, partner: Partner | None, **extra) -> dict:
    return {
        "request": request,
        "partner": partner,
        "bot_username": settings.BOT_USERNAME,
        "webapp_url": settings.WEBAPP_URL,
        "year": datetime.utcnow().year,
        "lang": _lang(request),
        **extra,
    }


def require_admin(request: Request, session: Session) -> Partner:
    """Гейт раздела /admin/* — ТОЛЬКО Николь по её Telegram (settings.ADMIN_TG_ID).
    Чужой партнёр или аноним → 404 (не 403: не раскрываем существование раздела).
    Здесь видны чувствительные данные по ВСЕМ агентам + финансовые ПД (реквизиты),
    поэтому доступ строго один аккаунт ([[plans/2026-06-02-partner-analytics-dashboard]])."""
    partner = current_partner(request, session)
    if partner is None or partner.telegram_id != settings.ADMIN_TG_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return partner


KOMMO_LEAD_URL = "https://primeadvice.kommo.com/leads/detail/"

# Ниже стольких заходивших агентов статистику использования показываем с плашкой
# «мало данных» — чтобы не делать выводов по 1–2 людям (план 2026-06-03).
USAGE_LOW_DATA_THRESHOLD = 5


def _humanize_survey(answers: dict) -> list[tuple[str, str]]:
    """Ответы анкеты L → [(вопрос, человекочитаемый ответ)] по белым спискам."""
    out: list[tuple[str, str]] = []
    for field in SURVEY_FIELD_ORDER:
        if field not in answers:
            continue
        label = SURVEY_LABELS.get(field, (field, field))[0]
        opts = {o[0]: o[1] for o in SURVEY_OPTIONS.get(field, [])}
        val = answers[field]
        if isinstance(val, list):
            human = ", ".join(opts.get(v, v) for v in val)
        else:
            human = opts.get(val, str(val))
        out.append((label, human))
    if answers.get("sphere_other"):
        out.append(("Сфера (другое)", str(answers["sphere_other"])))
    return out


@app.get("/admin/partner-stats", response_class=HTMLResponse)
def admin_partner_stats(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Дашборд лидеров (Фаза 2, план 2026-06-02). Только админ (Telegram-гейт)."""
    admin = require_admin(request, session)
    from sqlalchemy import case, distinct, func

    agg = (
        session.query(
            Lead.partner_id.label("pid"),
            func.count(Lead.id).label("total"),
            func.sum(case((Lead.status.in_(("new", "in_progress")), 1), else_=0)).label("active"),
            func.sum(case((Lead.status == "won", 1), else_=0)).label("won"),
            func.sum(case((Lead.status == "lost", 1), else_=0)).label("lost"),
            func.coalesce(func.sum(case((Lead.status == "won", Lead.amount_aed), else_=0)), 0).label("paid_sum"),
        )
        .group_by(Lead.partner_id)
        .all()
    )
    pids = [r.pid for r in agg if r.pid is not None]
    partners = (
        {p.id: p for p in session.query(Partner).filter(Partner.id.in_(pids)).all()}
        if pids else {}
    )
    rows = []
    for r in agg:
        p = partners.get(r.pid)
        if p is None:
            continue
        total, won = r.total or 0, r.won or 0
        rows.append({
            "id": p.id,
            "name": p.first_name or p.kommo_agent_name or f"#{p.id}",
            "ref_slug": p.ref_slug or "—",
            "total": total,
            "active": r.active or 0,
            "won": won,
            "lost": r.lost or 0,
            "paid_sum": float(r.paid_sum or 0),
            "conv": round(won / total * 100) if total else 0,
            "last_login_at": p.last_login_at,
            "onboarded": p.survey_completed_at is not None,
        })
    rows.sort(key=lambda x: (x["won"], x["total"], x["paid_sum"]), reverse=True)
    totals = {
        "agents": len(rows),
        "total": sum(x["total"] for x in rows),
        "active": sum(x["active"] for x in rows),
        "won": sum(x["won"] for x in rows),
        "lost": sum(x["lost"] for x in rows),
        "paid_sum": sum(x["paid_sum"] for x in rows),
    }

    # ── Использование портала (план 2026-06-03) ──────────────────────────────
    # По PageView: какие разделы открывают и кто из агентов сколько ходит.
    # Сортируем секции по числу УНИКАЛЬНЫХ агентов (важнее суммы заходов:
    # 100 заходов одного ≠ востребованность). Онбординг помечаем отдельно —
    # это вынужденный путь входа, не добровольный интерес.
    from app.usage import SECTION_LABELS
    sec_rows = (
        session.query(
            PageView.section,
            func.count(PageView.id).label("hits"),
            func.count(distinct(PageView.partner_id)).label("agents"),
        )
        .group_by(PageView.section)
        .all()
    )
    usage_sections = sorted(
        [
            {
                "label": SECTION_LABELS.get(s, s),
                "hits": h,
                "agents": a,
                "onboarding": s == "onboarding",
            }
            for s, h, a in sec_rows
        ],
        key=lambda x: (x["agents"], x["hits"]),
        reverse=True,
    )
    ua_rows = (
        session.query(
            PageView.partner_id,
            func.count(PageView.id).label("hits"),
            func.count(distinct(PageView.section)).label("sections"),
            func.max(PageView.created_at).label("last"),
        )
        .group_by(PageView.partner_id)
        .all()
    )
    upids = [r.partner_id for r in ua_rows]
    uparts = (
        {p.id: p for p in session.query(Partner).filter(Partner.id.in_(upids)).all()}
        if upids else {}
    )
    usage_agents = sorted(
        [
            {
                "id": r.partner_id,
                "name": (
                    (uparts[r.partner_id].first_name
                     or uparts[r.partner_id].kommo_agent_name
                     or f"#{r.partner_id}")
                    if r.partner_id in uparts else f"#{r.partner_id}"
                ),
                "hits": r.hits,
                "sections": r.sections,
                "last": r.last,
            }
            for r in ua_rows
        ],
        key=lambda x: x["hits"],
        reverse=True,
    )
    total_hits = session.query(func.count(PageView.id)).scalar() or 0
    total_visitors = session.query(func.count(distinct(PageView.partner_id))).scalar() or 0
    usage = {
        "sections": usage_sections,
        "agents": usage_agents,
        "total_hits": total_hits,
        "total_visitors": total_visitors,
        "low_data": total_visitors < USAGE_LOW_DATA_THRESHOLD,
    }

    return templates.TemplateResponse(
        "admin_partner_stats.html",
        _ctx(request, admin, rows=rows, totals=totals, usage=usage),
    )


@app.get("/admin/partner/{pid}", response_class=HTMLResponse)
def admin_partner_detail(pid: int, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Карточка агента (Фазы 2б/3/7): клиенты со ссылками на Kommo, профиль из
    анкеты и точный предпросмотр digest/win-отчёта. Только админ."""
    admin = require_admin(request, session)
    agent = session.get(Partner, pid)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    leads = (
        session.query(Lead)
        .filter_by(partner_id=pid)
        .order_by(Lead.created_at.desc())
        .all()
    )
    from app.notifications import build_digest_text, build_win_text

    digest_preview = build_digest_text(agent, session, datetime.utcnow())
    last_won = next((l for l in leads if l.status == "won"), None)
    win_preview = build_win_text(agent, last_won, session) if last_won else None
    return templates.TemplateResponse(
        "admin_partner_detail.html",
        _ctx(
            request, admin,
            agent=agent, leads=leads, kommo_url=KOMMO_LEAD_URL,
            profile=_humanize_survey(agent.onboarding_answers or {}),
            digest_preview=digest_preview, win_preview=win_preview,
        ),
    )


# ─── Атрибуция ссылок (план 2026-07-23): переходы + заявки + конверсия ───────
def build_attribution(session: Session, now: datetime | None = None) -> dict:
    """Отчёт атрибуции по персональным ссылкам: клики / заявки / конверсия.

    Окно якорим на первый клик (min link_clicks.created_at): клики стартуют с нуля при
    деплое, а QuizSubmission копятся исторически — без общего нижнего края конверсия
    вышла бы >100%. Клики И заявки считаем с одного `since`.

    Конверсию% даём ТОЛЬКО для 4 лендингов с квиз-стоком и ТОЛЬКО от квиз-кликов
    (не от чат-редиректов — те уводят в переписку, а не в форму). partner_bot и
    чат-редиректы — переходы без конверсии; для partner_bot ещё приходы из Referral."""
    now = now or datetime.utcnow()
    from sqlalchemy import func

    # Первый клик = нижний край окна. Берём ORM-объект (не func.min), чтобы значение
    # гарантированно было datetime (func.min на некоторых бэкендах отдаёт строку).
    first = session.query(LinkClick).order_by(LinkClick.created_at.asc()).first()
    since = first.created_at if first else None
    if since is None:
        return {"since": None, "by_content": [], "by_agent_content": []}

    ev_map = linkstat.content_event_slug_map()       # content_key → event_slug
    slug_to_key = {v: k for k, v in ev_map.items()}  # event_slug → content_key (None→consultation)

    # Клики по (content_key, surface) за окно.
    clicks: dict[str, dict[str, int]] = {}
    for ck, surf, n in (session.query(
            LinkClick.content_key, LinkClick.surface, func.count(LinkClick.id))
            .filter(LinkClick.created_at >= since)
            .group_by(LinkClick.content_key, LinkClick.surface).all()):
        clicks.setdefault(ck, {})[surf] = n

    # Заявки квиза по event_slug за окно → в content_key.
    leads_by_key: dict[str, int] = {}
    for ev, n in (session.query(QuizSubmission.event_slug, func.count(QuizSubmission.id))
                  .filter(QuizSubmission.created_at >= since)
                  .group_by(QuizSubmission.event_slug).all()):
        key = slug_to_key.get(ev)
        if key:
            leads_by_key[key] = leads_by_key.get(key, 0) + n

    # Приходы в бот (Referral, source='tg') за окно — метрика для partner_bot.
    arrivals = (session.query(func.count(Referral.id))
                .filter(Referral.created_at >= since, Referral.source == "tg").scalar()) or 0

    by_content = []
    for ck, label in linkstat.CONTENT_KEYS.items():
        surf = clicks.get(ck, {})
        quiz_clicks = surf.get("quiz", 0)
        chat_clicks = surf.get("tg", 0) + surf.get("wa", 0)
        bot_clicks = surf.get("bot", 0)
        is_landing = ck in linkstat.LANDING_KEYS
        leads = leads_by_key.get(ck, 0) if is_landing else 0
        conv = round(100.0 * leads / quiz_clicks, 1) if (is_landing and quiz_clicks) else None
        by_content.append({
            "key": ck, "label": label,
            "quiz_clicks": quiz_clicks, "chat_clicks": chat_clicks, "bot_clicks": bot_clicks,
            "total_clicks": quiz_clicks + chat_clicks + bot_clicks,
            "leads": leads, "conv": conv,
            "arrivals": arrivals if ck == "partner_bot" else None,
            "is_landing": is_landing,
        })
    by_content.sort(key=lambda r: r["total_clicks"], reverse=True)

    # По паре агент × контент: клики + заявки (только строки с partner_id).
    ac: dict[tuple[int, str], dict] = {}
    for pid, ck, n in (session.query(
            LinkClick.partner_id, LinkClick.content_key, func.count(LinkClick.id))
            .filter(LinkClick.created_at >= since, LinkClick.partner_id.isnot(None))
            .group_by(LinkClick.partner_id, LinkClick.content_key).all()):
        ac[(pid, ck)] = {"clicks": n, "leads": 0}
    for pid, ev, n in (session.query(
            QuizSubmission.partner_id, QuizSubmission.event_slug, func.count(QuizSubmission.id))
            .filter(QuizSubmission.created_at >= since, QuizSubmission.partner_id.isnot(None))
            .group_by(QuizSubmission.partner_id, QuizSubmission.event_slug).all()):
        key = slug_to_key.get(ev)
        if not key:
            continue
        ac.setdefault((pid, key), {"clicks": 0, "leads": 0})["leads"] += n

    pids = {pid for (pid, _) in ac}
    pmap = ({p.id: p for p in session.query(Partner).filter(Partner.id.in_(pids)).all()}
            if pids else {})
    by_agent_content = []
    for (pid, ck), v in ac.items():
        p = pmap.get(pid)
        if p is None:
            continue
        by_agent_content.append({
            "name": p.first_name or p.kommo_agent_name or f"#{pid}",
            "ref_slug": p.ref_slug or "—",
            "content": linkstat.CONTENT_KEYS.get(ck, ck),
            "clicks": v["clicks"], "leads": v["leads"],
        })
    by_agent_content.sort(key=lambda r: (r["clicks"], r["leads"]), reverse=True)

    return {"since": since, "by_content": by_content,
            "by_agent_content": by_agent_content[:60]}


@app.get("/admin/analytics", response_class=HTMLResponse)
def admin_analytics(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Аналитика персональных ссылок агентов (план 2026-07-23): переходы (LinkClick) →
    заявки → конверсия + сигналы здоровья. Только админ (Николь)."""
    admin = require_admin(request, session)
    attribution = build_attribution(session)
    # Живые сигналы (по БД, без HTTP) + статусы лендингов/целей из последнего прогона
    # джоба (self-проба ходит по HTTP, её на каждый показ админки не гоняем).
    from app import health as health_mod
    health_signals = health_mod.compute_signals(session)
    health_last = health_mod.LAST_RUN
    return templates.TemplateResponse(
        "admin_analytics.html",
        _ctx(request, admin, attribution=attribution,
             health_signals=health_signals, health_last=health_last),
    )


@app.get("/admin/sources", response_class=HTMLResponse)
def admin_sources(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Аналитика источников (Фаза 4): заявки по UTM / мероприятию / ссылке агента.
    Считаем КОНВЕРСИИ (заявки квиза + регистрации), не сырые клики. Только админ."""
    admin = require_admin(request, session)
    from sqlalchemy import func

    # 1) По UTM (source + campaign) — заявки квиза.
    utm = [
        {"src": r.src, "camp": r.camp, "n": r.n}
        for r in session.query(
            func.coalesce(QuizSubmission.utm_source, "—").label("src"),
            func.coalesce(QuizSubmission.utm_campaign, "—").label("camp"),
            func.count(QuizSubmission.id).label("n"),
        )
        .group_by(QuizSubmission.utm_source, QuizSubmission.utm_campaign)
        .order_by(func.count(QuizSubmission.id).desc())
        .all()
    ]

    # 2) По мероприятию: заявки квиза (NULL event_slug = консультация) + регистрации бота.
    events_quiz = [
        {"ev": r.ev, "n": r.n}
        for r in session.query(
            func.coalesce(QuizSubmission.event_slug, "consultation").label("ev"),
            func.count(QuizSubmission.id).label("n"),
        ).group_by(QuizSubmission.event_slug).order_by(func.count(QuizSubmission.id).desc()).all()
    ]
    events_reg = [
        {"ev": r.ev, "n": r.n}
        for r in session.query(
            EventRegistration.event_slug.label("ev"),
            func.count(EventRegistration.id).label("n"),
        ).group_by(EventRegistration.event_slug).order_by(func.count(EventRegistration.id).desc()).all()
    ]

    # 3) По ссылке агента: заявки квиза с привязкой к партнёру + сколько лидов won.
    quiz_by_p = dict(
        session.query(QuizSubmission.partner_id, func.count(QuizSubmission.id))
        .filter(QuizSubmission.partner_id.isnot(None))
        .group_by(QuizSubmission.partner_id)
        .all()
    )
    won_by_p = dict(
        session.query(Lead.partner_id, func.count(Lead.id))
        .filter(Lead.status == "won", Lead.partner_id.isnot(None))
        .group_by(Lead.partner_id)
        .all()
    )
    pids = list(quiz_by_p.keys())
    pmap = (
        {p.id: p for p in session.query(Partner).filter(Partner.id.in_(pids)).all()}
        if pids else {}
    )
    links = []
    for pid, n in quiz_by_p.items():
        p = pmap.get(pid)
        if p is None:
            continue
        links.append({
            "id": pid,
            "name": p.first_name or p.kommo_agent_name or f"#{pid}",
            "ref_slug": p.ref_slug or "—",
            "quiz": n,
            "won": won_by_p.get(pid, 0),
        })
    links.sort(key=lambda x: (x["quiz"], x["won"]), reverse=True)

    return templates.TemplateResponse(
        "admin_sources.html",
        _ctx(request, admin, utm=utm, events_quiz=events_quiz,
             events_reg=events_reg, links=links),
    )


# ─── Модуль выплат (план 2026-06-02, замена Excel менеджера) ─────────────────
# Менеджерский статус (4) → (агент-facing payout_state, payout_urgent). Агент в
# кабинете видит только дружелюбные in_calc/to_pay/paid; «срочно»/«уточняется» —
# внутренние для менеджера, агенту НЕ показываются.
PAYOUT_MGR_OPTIONS = [
    ("clarify", "Уточняется"),
    ("to_pay", "Под выплату"),
    ("urgent", "Срочно"),
    ("paid", "Оплачено"),
]
_MGR_TO_STATE = {
    "clarify": ("in_calc", False),
    "to_pay": ("to_pay", False),
    "urgent": ("to_pay", True),
    "paid": ("paid", False),
}


def _payout_mgr_value(lead: Lead) -> str:
    """Текущий менеджерский статус из (payout_state, payout_urgent)."""
    if getattr(lead, "payout_urgent", False):
        return "urgent"
    return {"in_calc": "clarify", "to_pay": "to_pay", "paid": "paid"}.get(
        lead.payout_state or "in_calc", "clarify"
    )


@app.get("/admin/payouts", response_class=HTMLResponse)
def admin_payouts(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Учёт выплат агентам (замена Excel). Только админ. Список won-лидов: авто из
    системы (клиент/Kommo/агент/сумма/дата) + ручные поля (комиссия, статус,
    договор, реквизиты, чек, дата выплаты)."""
    admin = require_admin(request, session)
    leads = (
        session.query(Lead)
        .filter(Lead.status == "won")
        .order_by(Lead.won_at.desc(), Lead.id.desc())
        .all()
    )
    pids = {l.partner_id for l in leads if l.partner_id}
    pmap = (
        {p.id: p for p in session.query(Partner).filter(Partner.id.in_(pids)).all()}
        if pids else {}
    )
    rows = [
        {"lead": l,
         "agent": (pmap[l.partner_id].first_name or pmap[l.partner_id].kommo_agent_name or "—")
                  if l.partner_id in pmap else "—",
         "mgr": _payout_mgr_value(l)}
        for l in leads
    ]
    fee_total = sum(float(l.fee_aed) for l in leads if l.fee_aed is not None)
    paid_total = sum(float(l.fee_aed) for l in leads
                     if l.fee_aed is not None and l.payout_state == "paid")
    return templates.TemplateResponse(
        "admin_payouts.html",
        _ctx(request, admin, rows=rows, kommo_url=KOMMO_LEAD_URL,
             mgr_options=PAYOUT_MGR_OPTIONS, fee_total=fee_total, paid_total=paid_total),
    )


@app.get("/admin/transfers", response_class=HTMLResponse)
def admin_transfers(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """Клиенты, переданные партнёрами: что уже в CRM, а что осталось только у нас.

    Сделку в Kommo по таким заявкам заводит менеджер руками, поэтому нужен список,
    по которому видно, что разобрано, а что нет — иначе единственный след заявки
    это сообщение в Telegram, которое тонет в переписке. Признак передачи —
    заполненный task_description (лиды из часового синка Kommo его не заполняют).
    """
    admin = require_admin(request, session)
    leads = (
        session.query(Lead)
        .filter(Lead.task_description.isnot(None), Lead.partner_id.isnot(None))
        .order_by(Lead.id.desc())
        .all()
    )
    pids = {l.partner_id for l in leads if l.partner_id}
    pmap = (
        {p.id: p for p in session.query(Partner).filter(Partner.id.in_(pids)).all()}
        if pids else {}
    )
    rows = [
        {"lead": l,
         "agent": partner_label(pmap[l.partner_id]) if l.partner_id in pmap else "—",
         "wa": _wa_digits(l.client_phone)}
        for l in leads
    ]
    in_crm = sum(1 for r in rows if r["lead"].kommo_lead_id)
    return templates.TemplateResponse(
        "admin_transfers.html",
        _ctx(request, admin, rows=rows, kommo_url=KOMMO_LEAD_URL,
             in_crm=in_crm, not_in_crm=len(rows) - in_crm),
    )


# Метка источника — String(16) в channel_subscribers, префикс длиннее колонки
# не совпадёт ни с чем. Потолок — только чтобы не гонять километровый LIKE.
CHANNEL_TAG_PREFIX_MAX = 32


def _channel_tags_token_ok(request: Request) -> bool:
    """Вторая дверь к счётчикам: заголовок `X-Api-Token`. Нужна машине ARDORIUM —
    у неё нет браузера и cookie владельца, а забирает она цифры по расписанию.

    Пустая настройка = двери НЕТ: иначе незаполненная переменная в Railway
    открыла бы адрес любому, кто пришлёт пустой заголовок. Сравнение постоянного
    времени, и в байтах, а не в строках: `compare_digest` на строке с не-ASCII
    бросает TypeError, а в заголовке может приехать что угодно.
    """
    expected = settings.CHANNEL_TAGS_TOKEN
    if not expected:
        return False
    given = request.headers.get("X-Api-Token") or ""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


@app.get("/admin/api/channel-tags")
def admin_api_channel_tags(request: Request,
                           since: str | None = None,
                           prefix: str = "dl:",
                           session: Session = Depends(get_session)) -> JSONResponse:
    """Счётчики привратника канала по меткам рассылки (план 2026-09-04).

    Кто дошёл до канала по ссылке рассылки, знает только бот. Здесь эти цифры
    забирает по расписанию ARDORIUM и показывает на карточке рассылки.

    Наружу уезжают ТОЛЬКО метка, шесть чисел и две даты (см. tag_counts). Ни
    telegram_id, ни имени, ни пригласительной ссылки: адрес читает чужой сервис.

    Двери две: cookie владельца (браузер Николь) или токен в заголовке (машина).
    Чужому — 404, как и остальной админке: не подтверждаем, что адрес есть.
    Только чтение, `no-store`: цифры живые, промежуточным кэшам их не отдаём.
    """
    if not _channel_tags_token_ok(request):
        require_admin(request, session)     # чужой/аноним → 404 внутри
    since_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since.strip(), "%Y-%m-%d")
        except ValueError:
            # Пришедший сюда уже показал право, так что подсказка формата ничего
            # не выдаёт. Молча игнорировать кривую дату нельзя: расписание
            # ARDORIUM годами тянуло бы полную выборку и не знало об этом.
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "since: ожидается YYYY-MM-DD")
    # Пустой prefix — это «все метки», а не «весь список источников»: заявки из
    # канала (jr:/join_request) к рассылке отношения не имеют.
    tag_prefix = (prefix or "").strip()[:CHANNEL_TAG_PREFIX_MAX] or "dl:"
    from app import channel_gate          # локально: тянет aiogram, вебу он не нужен
    return JSONResponse(
        {"generated_at": channel_gate.iso_utc(datetime.utcnow()),
         "rows": channel_gate.tag_counts(session, prefix=tag_prefix, since=since_dt)},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/payouts/{lead_id}")
def admin_payout_save(
    lead_id: int,
    request: Request,
    fee_aed: str = Form(""),
    mgr_status: str = Form("clarify"),
    agreement_url: str = Form(""),
    bank_details: str = Form(""),
    receipt_url: str = Form(""),
    paid_on: str = Form(""),
    session: Session = Depends(get_session),
):
    """Сохранить выплату по won-лиду. Только админ. Менеджерский статус → агент-
    facing payout_state + флаг urgent. Реквизиты — финансовые ПД, в лог не пишем."""
    require_admin(request, session)
    lead = session.get(Lead, lead_id)
    if lead is None or lead.status != "won":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Пробел и запятая — разделители тысяч (формат файла: «1,460», «2 320»,
    # «3,424.05»), точка — десятичная. Убираем тысячные, точку сохраняем.
    fee = (fee_aed or "").strip().replace(" ", "").replace(",", "")
    if fee:
        try:
            lead.fee_aed = float(fee)
        except ValueError:
            pass  # мусор в сумме игнорируем, остальное сохраняем
    else:
        lead.fee_aed = None
    state, urgent = _MGR_TO_STATE.get((mgr_status or "").strip(), ("in_calc", False))
    lead.payout_state = state
    lead.payout_urgent = urgent
    lead.agreement_url = (agreement_url or "").strip() or None
    lead.bank_details = (bank_details or "").strip() or None
    lead.payout_receipt_url = (receipt_url or "").strip() or None
    lead.payout_paid_on = (paid_on or "").strip() or None
    session.commit()
    return RedirectResponse("/admin/payouts", status_code=303)


def l2_total(partner: Partner) -> float:
    """Суммарный доход партнёра 2-го уровня (AED). Источник истины — разбивка
    по суб-агентам l2_income (список [{name, aed}, …]); если её нет, падаем на
    старое число l2_income_aed. Так «Заработано» всегда сходится с тем, что
    показано в строке «2-й уровень»."""
    items = getattr(partner, "l2_income", None)
    if items:
        return float(sum((x.get("aed") or 0) for x in items))
    return float(getattr(partner, "l2_income_aed", None) or 0)


def _balance_kpi(session: Session, partner: Partner) -> dict:
    """Минимум данных для верхней «балансовой» полосы (шаблон _balance.html):
    заработано + ожидаемое вознаграждение по числу заявок + код партнёра.
    Используется на /leads, /tools, /products. На дашборде те же ключи считаются
    вместе с полным kpi, поэтому полоса там работает на своём наборе данных."""
    leads_q = session.query(Lead).filter_by(partner_id=partner.id)
    leads_count = leads_q.count()
    # «Ожидаемое вознаграждение» — только по АКТИВНЫМ заявкам (new/in_progress):
    # отказы и уже оплаченные не обещают будущих денег (решение Николь 2026-07-21;
    # раньше множилось на все заявки и завышало ожидания).
    active_count = leads_q.filter(Lead.status.in_(["new", "in_progress"])).count()
    won_rows = leads_q.filter(Lead.status == "won").all()
    # «Заработано» — комиссия ПАРТНЁРА по оплаченным лидам (решение Николь
    # 2026-07-21), а не сумма чеков клиентов: партнёру показываем его доход,
    # чужой оборот его не касается. NULL-комиссии дают 0. Плюс доход 2-го уровня
    # (l2_total) — комиссия за суб-агентов, не привязана к лидам.
    # float() на каждом слагаемом обязателен: commission_aed приходит из Postgres
    # как Decimal, l2_total() возвращает float, а Decimal + float — TypeError
    # (даже когда float равен 0.0). Без этого /dashboard падал 500 у любого
    # партнёра с хотя бы одной заполненной комиссией.
    earned_aed = (sum(float(getattr(l, "commission_aed", None) or 0) for l in won_rows)
                  + float(l2_total(partner)))
    return {
        "leads": leads_count,
        "active_leads": active_count,
        "earned_aed": float(earned_aed),
        "expected_usd_low": active_count * 300,
        "expected_usd_high": active_count * 1000,
        # Средняя комиссия — для серого ориентира в столбце «Ваша комиссия»
        # (_leads_table.html). Балансовую полосу НЕ трогает.
        "avg_commission_aed": float(AVG_COMMISSION_AED),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if partner:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


# ─── Квиз-лендинг «Консультация» (план 2026-06-02) ───────────────────────────
# Публичный квиз: 3 вопроса → имя+телефон → лид в Kommo воронку 1.1 + Postgres.
# Атрибуция к агенту по ?ref=<ref_slug>. Запись в Kommo под предохранителем
# settings.QUIZ_KOMMO_LIVE (см. app/kommo_lead.py).

def _quiz_mask_phone(norm: str) -> str:
    return f"{norm[:4]}***{norm[-2:]}" if len(norm) > 6 else "***"


@app.get("/partners", response_class=HTMLResponse)
def partners_page(request: Request) -> HTMLResponse:
    """Публичная страница о партнёрской программе (план 2026-07-27, Фаза 1).
    Заменяет главную старого сайта на Тильде (oncount.co). Весь текст — в
    app/partners_config.py. С ?ref= кнопка «Стать партнёром» ведёт на /p/<ref>:
    приглашение закрепляется за пригласившим агентом (2-й уровень), а не теряется."""
    from app import partners_config as pc
    ref = linkstat.sanitize_ref(request.query_params.get("ref"))
    linkstat.record_click("partners_page", "quiz", ref, request.headers.get("user-agent"))
    lang = _lang(request)
    return templates.TemplateResponse("partners.html", {
        "request": request,
        # Калькулятор и блок «кто работает с клиентом» — те же, что в кабинете:
        # цифры из calc_config, бухгалтеры из ACCOUNTANTS. Два источника правды
        # здесь развели бы прайс на лендинге и в кабинете.
        "lang": lang,
        "calc": calc_data(lang),
        "accountants": ACCOUNTANTS,
        # tr(значение) — выбор языка для двуязычных полей конфига (ru/en).
        "tr": lambda value: pc.t(value, lang),
        "hero": pc.HERO,
        "meeting": pc.MEETING,
        "why": pc.WHY,
        "benefit": pc.BENEFIT,
        "audience": pc.AUDIENCE,
        "steps": pc.STEPS,
        "reviews": pc.REVIEWS,
        "client_value": pc.CLIENT_VALUE,
        "guarantee": pc.GUARANTEE,
        "licence": pc.LICENCE,
        "insurance": pc.INSURANCE,
        "team_title": pc.TEAM_TITLE,
        "final": pc.FINAL,
        "contacts": pc.CONTACTS,
        "legal_links": pc.LEGAL_LINKS,
        "bullet_icon": pc.BULLET_ICON,
        "ref": ref,
        "join_href": f"/p/{ref}" if ref else "/join",
    })


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request) -> HTMLResponse:
    """Лендинг интенсива «Бизнес-ассистент за 5 дней» (2026-08-03). Формы нет:
    каждая кнопка ведёт в личный Telegram Николь с готовым первым сообщением
    (решение Николь), поэтому QuizSubmission отсюда не появляется — считаем
    только переходы. Тексты и цены — app/assistant_config.py."""
    from app import assistant_config as ac
    from app import partners_config as pc
    from app import seller_config
    linkstat.record_click("assistant", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("assistant.html", {
        "request": request,
        **ac.page(),
        # Футер — тот же, что на /partners: один источник адреса и контактов.
        "contacts": pc.CONTACTS,
        # Политика ПРОДАВЦА (ИП), а не ONCOUNT: покупатель интенсива заключает
        # договор с ИП, и в подвале должен быть документ именно этого лица.
        "legal_links": [("Оферта", "/assistant/offer"),
                        ("Условия возврата", "/assistant/refund"),
                        ("Политика конфиденциальности", "/assistant/policy")],
        # Реквизиты продавца рублёвых оплат: None, пока не заполнены (тогда блок
        # в подвале просто не рендерится).
        "seller": seller_config.seller(),
    })


# ─── Оплата: три способа + заявление «я оплатил» (план 2026-08-03) ────────────
# Платёжного слоя в ядре нет: страница показывает реквизиты, а факт зачисления
# сверяет человек по выписке. Поэтому здесь нет ни платёжного провайдера, ни
# вебхуков — только PaymentClaim (слово клиента) и карточка в Telegram.

@app.get("/pay", response_class=HTMLResponse)
def pay_page(request: Request) -> HTMLResponse:
    """Страница оплаты: рубли / международная карта / крипта. Тексты, цены и
    реквизиты — app/pay_config.py (в вёрстке их нет). Ссылку выдаёт менеджер, в
    поиск страница не отдаётся (noindex в шаблоне)."""
    from app import pay_config
    linkstat.record_click("pay", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("pay.html", {
        "request": request,
        "page_title": "ONCOUNT — оплата",
        **pay_config.page(),
    })


@app.post("/pay/confirm")
async def pay_confirm(request: Request,
                      session: Session = Depends(get_session)) -> dict:
    """Приём заявления об оплате → Postgres + карточка в Telegram.

    Клиенту ВСЕГДА отвечаем ok: он уже перевёл деньги, и сообщение об ошибке
    приёма здесь читается как «платёж не прошёл» — лишняя паника на ровном месте.
    Порядок: honeypot → валидация → дедуп → запись → уведомление.

    Что НЕ доверяем клиенту: сумму (берём снимок из конфига по способу) и способ
    (только белый список pay_config.VALID_METHODS). Сырой ввод обратно не рендерим.
    """
    from app import pay_config

    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    # Honeypot: поле website видно только ботам — заполнено → тихо «ok».
    if (data.get("website") or "").strip():
        return {"ok": True}

    method = pay_config.method_by_id(data.get("method"))
    if method is None or not pay_config.is_ready(method):
        # Способа нет в белом списке либо по нему нельзя платить (реквизиты не
        # заполнены) — заявления о такой оплате быть не может.
        return {"ok": False, "error": "method"}

    name = (data.get("name") or "").strip()[:200] or None
    phone_norm = normalize_phone(data.get("phone") or "")
    if len(phone_norm) < PHONE_MIN_DIGITS:
        return {"ok": False, "error": "phone"}

    def _s(key, n=180):
        v = data.get(key)
        return v.strip()[:n] if isinstance(v, str) and v.strip() else None

    product_slug = str(pay_config.PRODUCT.get("slug") or "product")[:64]
    ref_slug = _s("ref", 16)
    # Ровно под String(128) колонок payment_claims: значение длиннее колонки
    # роняет commit → заявление теряется целиком (тот же класс бага, что 502 по
    # link_key=16 в истории репо).
    utm = {k: _s(k, 128) for k in ("utm_source", "utm_medium", "utm_campaign",
                                   "utm_content", "utm_term")}

    # Дедуп: то же заявление (телефон + продукт) за 2 минуты — двойной тап по
    # кнопке или повторная отправка, а не вторая оплата.
    recent = (session.query(PaymentClaim)
              .filter(PaymentClaim.phone == phone_norm,
                      PaymentClaim.product_slug == product_slug,
                      PaymentClaim.created_at >= datetime.utcnow() - timedelta(minutes=2))
              .first())
    if recent is not None:
        return {"ok": True}

    partner = None
    if ref_slug:
        partner = session.query(Partner).filter_by(ref_slug=ref_slug).first()

    claim = PaymentClaim(
        product_slug=product_slug,
        method=method["id"],
        # Снимок цены — из конфига, не из формы: сумму в нашей записи клиент
        # подставлять не должен.
        amount_label=(pay_config.price_of(method) or None),
        name=name, phone=phone_norm,
        contact=_s("contact", 200), note=_s("note", 600),
        ref_slug=ref_slug, partner_id=partner.id if partner else None,
        referrer=_s("referrer", 400), landing_url=_s("landing_url", 400),
        status="new", **{k: v for k, v in utm.items()},
    )
    session.add(claim)
    session.commit()

    _notify_admin_payment(claim, partner)
    log.info("payment claim product=%s method=%s phone=%s agent=%s",
             product_slug, method["id"], _quiz_mask_phone(phone_norm),
             partner.id if partner else "-")
    return {"ok": True}


def _notify_admin_payment(claim: "PaymentClaim", partner: "Partner | None") -> None:
    """Карточка оплаты владельцу и менеджерам. Best-effort: провал уведомления не
    влияет на приём (строка уже в БД). Телефон шлём полностью — по нему сверяют
    платёж и открывают доступ. Комментарий клиента — сырой текст, поэтому уходит
    только в Telegram (в HTML мы его не рендерим)."""
    from app import pay_config
    lines = [pay_config.NOTIFY_HEADER, ""]
    lines.append(f"Продукт: {pay_config.PRODUCT.get('title') or claim.product_slug}")
    lines.append(f"Способ: {pay_config.METHOD_TITLES.get(claim.method, claim.method)}")
    lines.append(f"Сумма на странице: {claim.amount_label or '—'}")
    lines.append("")
    lines.append(f"Имя: {claim.name or '—'}")
    lines.append(f"Телефон: +{claim.phone}")
    lines.append(f"Написать: https://wa.me/{claim.phone}")
    if claim.contact:
        lines.append(f"Контакт: {claim.contact}")
    if claim.note:
        lines.append(f"Комментарий: {claim.note}")
    lines.append("")
    if partner:
        lines.append(f"Агент: {partner.kommo_agent_name or partner.first_name or partner.ref_slug}")
    elif claim.ref_slug:
        lines.append(f"Реф-метка (агент не найден): {claim.ref_slug}")
    lines.append("")
    lines.append("⚠️ Это СЛОВО клиента, а не подтверждённый платёж — сверьте по выписке.")
    _tg_send_lead("\n".join(lines))


@app.get("/policy", response_class=HTMLResponse)
def policy_page(request: Request) -> HTMLResponse:
    """Политика конфиденциальности ONCOUNT (план 2026-07-27, Фаза 2). Со старого
    сайта НЕ переносили: там лежала политика ggacadem.com — чужой проект."""
    from app import legal_config as lc
    from app import partners_config as pc
    lang = _lang(request)
    return templates.TemplateResponse("legal.html", {
        "request": request,
        "lang": lang,
        "tr": lambda value: pc.t(value, lang),
        "doc": lc.POLICY,
        "contacts": pc.CONTACTS,
    })


@app.get("/assistant/policy", response_class=HTMLResponse)
def assistant_policy_page(request: Request) -> HTMLResponse:
    """Политика конфиденциальности ПРОДАВЦА интенсива (ИП, 152-ФЗ).

    Отдельно от /policy: там политика ONCOUNT — компании в ОАЭ. Интенсив продаёт
    ИП по российскому праву, и покупателю нельзя показывать документ чужого лица.
    """
    from app import legal_ip_config as lic
    from app import partners_config as pc
    return templates.TemplateResponse("legal.html", {
        "request": request,
        "lang": "ru",
        "tr": lambda value: pc.t(value, "ru"),
        "doc": lic.policy(),
    })


@app.get("/assistant/offer", response_class=HTMLResponse)
def assistant_offer_page(request: Request) -> HTMLResponse:
    """Договор публичной оферты на интенсив (ИП, право РФ)."""
    from app import legal_offer_config as loc
    from app import partners_config as pc
    return templates.TemplateResponse("legal.html", {
        "request": request, "lang": "ru",
        "tr": lambda value: pc.t(value, "ru"),
        "doc": loc.OFFER,
    })


@app.get("/assistant/refund", response_class=HTMLResponse)
def assistant_refund_page(request: Request) -> HTMLResponse:
    """Условия возврата — неотъемлемая часть оферты."""
    from app import legal_offer_config as loc
    from app import partners_config as pc
    return templates.TemplateResponse("legal.html", {
        "request": request, "lang": "ru",
        "tr": lambda value: pc.t(value, "ru"),
        "doc": loc.REFUND,
    })


@app.get("/consultation", response_class=HTMLResponse)
def consultation_page(request: Request) -> HTMLResponse:
    from app import quiz_config
    lang = _lang(request)
    linkstat.record_click("consultation", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "lang": lang,
        "page_title": "ONCOUNT — free tax consultation" if lang == "en" else None,
        **quiz_config.page(lang),
        "submit_url": "/consultation/submit",
        "accountants": ACCOUNTANTS,
    })


@app.post("/consultation/submit")
async def consultation_submit(request: Request,
                              session: Session = Depends(get_session)) -> dict:
    """Приём заявки квиза /consultation → лид в воронку 1.1 + Postgres + TG-пуш."""
    from app import quiz_config
    return await _handle_quiz_submit(
        request, session,
        valid_options=quiz_config.VALID_OPTIONS,
        question_titles=quiz_config.QUESTION_TITLES,
        event_slug=None,
        notify_header="🟢 Новая заявка с квиза /consultation",
        lead_prefix="Квиз-консультация",
        lead_tag="quiz",
        note_intro="Заявка с квиз-лендинга /consultation.",
        deliver_wa_text=quiz_config.CONFIRM_WA_TEXT,
        deliver_wa_text_en=quiz_config.CONFIRM_WA_TEXT_EN,
    )


# ─── Мастер-класс с главбухом: обложка-оффер + те же 3 вопроса (план 2026-06-02) ──

@app.get("/mk", response_class=HTMLResponse)
def mk_page(request: Request) -> HTMLResponse:
    from app import mk_config
    lang = _lang(request)
    linkstat.record_click("mk", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "lang": lang,
        "page_title": ("ONCOUNT — masterclass with the chief accountant"
                       if lang == "en" else "ONCOUNT — мастер-класс с главбухом"),
        **mk_config.page(lang),
        "submit_url": "/mk/submit",
    })


@app.post("/mk/submit")
async def mk_submit(request: Request,
                    session: Session = Depends(get_session)) -> dict:
    """Приём регистрации на мастер-класс → лид в воронку 1.1 + Postgres + TG-пуш.
    Та же машинерия, что у /consultation, но с event_slug и своими текстами лида."""
    from app import mk_config
    return await _handle_quiz_submit(
        request, session,
        valid_options=mk_config.VALID_OPTIONS,
        question_titles=mk_config.QUESTION_TITLES,
        event_slug=mk_config.EVENT_SLUG,
        notify_header="🎓 Новая регистрация на мастер-класс (30 июля)",
        lead_prefix=mk_config.KOMMO_LEAD_PREFIX,
        lead_tag=mk_config.KOMMO_LEAD_TAG,
        note_intro=mk_config.KOMMO_NOTE_INTRO,
        deliver_wa_text=mk_config.CONFIRM_WA_TEXT,
        deliver_wa_text_en=mk_config.CONFIRM_WA_TEXT_EN,
    )


# ─── Лид-магнит «0% Corporate Tax»: квиз → PDF чек-листа ссылкой в WhatsApp ──────
# (план 2026-06-02). Партнёрский канал: тизер + ссылка ?ref={код}. Те же 3 шага,
# но после заявки клиенту уходит WhatsApp-сообщение со ссылкой на PDF.

@app.get("/guide/corp-tax", response_class=HTMLResponse)
def guide_corp_tax_page(request: Request) -> HTMLResponse:
    from app import leadmagnet_config as lm
    lang = _lang(request)
    linkstat.record_click("leadmagnet_corptax", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "lang": lang,
        "page_title": ("ONCOUNT — 0% Corporate Tax checklist"
                       if lang == "en" else "ONCOUNT — чек-лист 0% Corporate Tax"),
        **lm.page(lang),
        "submit_url": "/guide/corp-tax/submit",
    })


@app.post("/guide/corp-tax/submit")
async def guide_corp_tax_submit(request: Request,
                                session: Session = Depends(get_session)) -> dict:
    """Приём заявки лид-магнита → лид в воронку 1.1 + PDF-ссылка в WhatsApp.
    Та же машинерия, что у /consultation и /mk, плюс доставка чек-листа ссылкой."""
    from app import leadmagnet_config as lm
    return await _handle_quiz_submit(
        request, session,
        valid_options=lm.VALID_OPTIONS,
        question_titles=lm.QUESTION_TITLES,
        event_slug=lm.EVENT_SLUG,
        notify_header="📥 Новая заявка с лид-магнита «0% Corporate Tax»",
        lead_prefix=lm.KOMMO_LEAD_PREFIX,
        lead_tag=lm.KOMMO_LEAD_TAG,
        note_intro=lm.KOMMO_NOTE_INTRO,
        deliver_wa_text=lm.WA_TEXT.replace("{link}", lm.GUIDE_PDF_URL),
        deliver_wa_text_en=lm.WA_TEXT_EN.replace("{link}", lm.GUIDE_PDF_URL_EN),
    )


# ─── Лид-магнит «5 ошибок при открытии бизнеса» (2026-06-03) ────────────────────
# Тот же движок, что у /guide/corp-tax, но другая тема и улучшение: после заявки
# клиенту уходит ПЕРСОНАЛИЗИРОВАННОЕ WhatsApp-сообщение (ссылка на PDF + подсказка,
# какая из 5 ошибок ближе к его зоне риска — по ответу `worry`). Текст строит
# leadmagnet5_config.wa_text(answers) через deliver_wa_text_builder.

@app.get("/guide/5-mistakes", response_class=HTMLResponse)
def guide_5mistakes_page(request: Request) -> HTMLResponse:
    from app import leadmagnet5_config as lm5
    lang = _lang(request)
    linkstat.record_click("leadmagnet_5mistakes", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "lang": lang,
        "page_title": ("ONCOUNT — 5 mistakes when starting a business in the UAE"
                       if lang == "en" else "ONCOUNT — чек-лист «5 ошибок при открытии бизнеса в ОАЭ»"),
        **lm5.page(lang),
        "submit_url": "/guide/5-mistakes/submit",
    })


@app.post("/guide/5-mistakes/submit")
async def guide_5mistakes_submit(request: Request,
                                 session: Session = Depends(get_session)) -> dict:
    """Приём заявки лид-магнита «5 ошибок» → лид в воронку 1.1 + персональная
    PDF-ссылка в WhatsApp. Тот же движок, плюс билдер персонального WA-текста."""
    from app import leadmagnet5_config as lm5
    return await _handle_quiz_submit(
        request, session,
        valid_options=lm5.VALID_OPTIONS,
        question_titles=lm5.QUESTION_TITLES,
        event_slug=lm5.EVENT_SLUG,
        notify_header="📥 Новая заявка с лид-магнита «5 ошибок при открытии бизнеса»",
        lead_prefix=lm5.KOMMO_LEAD_PREFIX,
        lead_tag=lm5.KOMMO_LEAD_TAG,
        note_intro=lm5.KOMMO_NOTE_INTRO,
        deliver_wa_text=lm5.WA_TEXT.replace("{link}", lm5.GUIDE_PDF_URL),  # фоллбэк
        deliver_wa_text_builder=lm5.wa_text,                        # персонализация
        deliver_wa_text_en=lm5.WA_TEXT_EN.replace("{link}", lm5.GUIDE_PDF_URL_EN),
        deliver_wa_text_builder_en=lm5.wa_text_en,
    )


# ─── Лид-магнит «5 ошибок при открытии бизнеса» (2026-06-03) ────────────────────
# Тот же движок, что у /guide/corp-tax, но другая тема и улучшение: после заявки
# клиенту уходит ПЕРСОНАЛИЗИРОВАННОЕ WhatsApp-сообщение (ссылка на PDF + подсказка,
# какая из 5 ошибок ближе к его зоне риска — по ответу `worry`). Текст строит
# leadmagnet5_config.wa_text(answers) через deliver_wa_text_builder.

@app.get("/guide/5-mistakes", response_class=HTMLResponse)
def guide_5mistakes_page(request: Request) -> HTMLResponse:
    from app import leadmagnet5_config as lm5
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "page_title": "ONCOUNT — чек-лист «5 ошибок при открытии бизнеса в ОАЭ»",
        "cover": lm5.COVER,
        "intro": lm5.INTRO,
        "questions": lm5.QUESTIONS,
        "final": lm5.FINAL,
        "thanks": lm5.THANKS,
        "socials": lm5.SOCIALS,
        "submit_url": "/guide/5-mistakes/submit",
    })


@app.post("/guide/5-mistakes/submit")
async def guide_5mistakes_submit(request: Request,
                                 session: Session = Depends(get_session)) -> dict:
    """Приём заявки лид-магнита «5 ошибок» → лид в воронку 1.1 + персональная
    PDF-ссылка в WhatsApp. Тот же движок, плюс билдер персонального WA-текста."""
    from app import leadmagnet5_config as lm5
    return await _handle_quiz_submit(
        request, session,
        valid_options=lm5.VALID_OPTIONS,
        question_titles=lm5.QUESTION_TITLES,
        event_slug=lm5.EVENT_SLUG,
        notify_header="📥 Новая заявка с лид-магнита «5 ошибок при открытии бизнеса»",
        lead_prefix=lm5.KOMMO_LEAD_PREFIX,
        lead_tag=lm5.KOMMO_LEAD_TAG,
        note_intro=lm5.KOMMO_NOTE_INTRO,
        deliver_wa_text=lm5.WA_TEXT.replace("{link}", lm5.GUIDE_PDF_URL),  # фоллбэк
        deliver_wa_text_builder=lm5.wa_text,                        # персонализация
    )


# ─── Лид-магниты по всем чек-листам (2026-07-27): единый реестр ────────────────
# Темы описаны данными в leadmagnet_topics.TOPICS, роуты — универсальные.
# Регистрируются ПОСЛЕ выделенных /guide/corp-tax и /guide/5-mistakes, поэтому
# фиксированные пути продолжают обслуживаться своими обработчиками.
# ⚠️ В кабинет (/links) ссылки на эти квизы НЕ добавлены — решение Николь
# 2026-07-27 «в партнёрский кабинет пока не выкладывать».

@app.get("/guide/{lm_slug}", response_class=HTMLResponse)
def guide_topic_page(lm_slug: str, request: Request) -> HTMLResponse:
    from app import leadmagnet_topics as lmt
    if lm_slug not in lmt.TOPICS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    lang = _lang(request)
    linkstat.record_click(f"leadmagnet_{lm_slug.replace('-', '_')}", "quiz",
                          request.query_params.get("ref"), request.headers.get("user-agent"))
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "lang": lang,
        "page_title": lmt.page_title(lm_slug, lang),
        **lmt.page(lm_slug, lang),
        "submit_url": f"/guide/{lm_slug}/submit",
    })


@app.post("/guide/{lm_slug}/submit")
async def guide_topic_submit(lm_slug: str, request: Request,
                             session: Session = Depends(get_session)) -> dict:
    """Приём заявки лид-магнита из реестра тем → лид в воронку 1.1 +
    PDF-ссылка в WhatsApp. Машинерия та же, что у выделенных лид-магнитов."""
    from app import leadmagnet_topics as lmt
    if lm_slug not in lmt.TOPICS:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    marks = lmt.kommo(lm_slug)
    return await _handle_quiz_submit(
        request, session,
        valid_options=lmt.VALID_OPTIONS,
        question_titles=lmt.QUESTION_TITLES,
        event_slug=marks["event_slug"],
        notify_header=marks["header"],
        lead_prefix=marks["prefix"],
        lead_tag=marks["tag"],
        note_intro=marks["note"],
        deliver_wa_text=lmt.wa_text(lm_slug, "ru"),
        deliver_wa_text_en=lmt.wa_text(lm_slug, "en"),
    )


async def _handle_quiz_submit(
    request: Request, session: Session, *,
    valid_options: dict, question_titles: dict,
    event_slug: str | None, notify_header: str,
    lead_prefix: str, lead_tag: str, note_intro: str,
    deliver_wa_text: str | None = None,
    deliver_wa_text_builder=None,
    deliver_wa_text_en: str | None = None,
    deliver_wa_text_builder_en=None,
) -> dict:
    """Общее ядро приёма заявок квиз-лендингов (/consultation и /mk). Клиенту
    ВСЕГДА отвечаем ok (идемпотентно, без утечки внутренней логики). Порядок:
    валидация → honeypot/дедуп → Postgres → Kommo (под гардом) → TG-пуш админу.
    Сырой ввод НЕ рендерим обратно (анти-XSS)."""
    from app.kommo_lead import create_consultation_lead

    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    # Honeypot: поле website видно только ботам — заполнено → тихо «ok», ничего не пишем.
    if (data.get("website") or "").strip():
        return {"ok": True}

    name = (data.get("name") or "").strip()[:200] or None
    phone_norm = normalize_phone(data.get("phone") or "")
    if len(phone_norm) < PHONE_MIN_DIGITS:
        return {"ok": False, "error": "phone"}

    # Ответы — только белый список вариантов (анти-инъекция произвольных строк).
    raw_answers = data.get("answers") or {}
    answers = {}
    if isinstance(raw_answers, dict):
        for qid, valid in valid_options.items():
            v = raw_answers.get(qid)
            if isinstance(v, str) and v in valid:
                answers[qid] = v

    def _s(key, n=180):
        v = data.get(key)
        return v.strip()[:n] if isinstance(v, str) and v.strip() else None

    ref_slug = _s("ref", 16)
    # Ровно под String(128) колонок quiz_submissions: обрезка длиннее колонки
    # роняла commit → заявка терялась целиком, клиент видел «спасибо»
    # (тот же класс бага, что 502 по link_key; находка аудита 2026-07-06).
    utm = {k: _s(k, 128) for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")}

    # Дедуп в пределах ОДНОГО события: та же заявка (телефон+event_slug) за 2 минуты
    # → не плодим строку/лид. Разные события (МК vs консультация) не глушим.
    recent = (session.query(QuizSubmission)
              .filter(QuizSubmission.phone == phone_norm,
                      QuizSubmission.event_slug == event_slug,
                      QuizSubmission.created_at >= datetime.utcnow() - timedelta(minutes=2))
              .first())
    if recent is not None:
        return {"ok": True}

    # Атрибуция агента по ref_slug → Partner (строго по совпадению, не угадываем).
    partner = None
    if ref_slug:
        partner = session.query(Partner).filter_by(ref_slug=ref_slug).first()

    sub = QuizSubmission(
        name=name, phone=phone_norm, answers=answers or None, event_slug=event_slug,
        ref_slug=ref_slug, partner_id=partner.id if partner else None,
        referrer=_s("referrer", 400), landing_url=_s("landing_url", 400),
        kommo_status="pending", **{k: v for k, v in utm.items()},
    )
    session.add(sub)
    session.commit()

    # Kommo воронка 1.1 (под предохранителем). Лид не создаётся — заявка уже в БД.
    agent_enum_id = partner.kommo_agent_enum_id if partner else None
    result = create_consultation_lead(
        name=name, phone_norm=phone_norm, answers=answers,
        agent_enum_id=agent_enum_id, utm=utm, ref_slug=ref_slug,
        lead_prefix=lead_prefix, lead_tag=lead_tag, note_intro=note_intro,
        question_titles=question_titles,
    )
    sub.kommo_status = result["status"]
    sub.kommo_lead_id = result.get("kommo_lead_id")
    session.commit()

    # Клиентское WhatsApp-сообщение после заявки: PDF лид-магнита ИЛИ авто-
    # подтверждение (/mk, /consultation — решение Николь 2026-07-21). Уходит с
    # клиентского канала (номер 84, WAZZUP_CLIENT_CHANNEL_ID; фолбэк — сервисный).
    # Best-effort: лид уже создан, провал доставки не валит приём. Предохранители —
    # внутри send_wa_text (dev / WAZZUP_TEST_ONLY_NUMBER).
    # deliver_wa_text_builder(answers) → персональный текст (например, подсказка по
    # релевантной ошибке у /guide/5-mistakes); ошибка билдера → фоллбэк на статичный.
    # Клиент с EN-лендинга (?lang=en, квиз кладёт lang в payload) получает
    # EN-текст, если вызывающий его передал; иначе — русский.
    client_lang = "en" if data.get("lang") == "en" else "ru"
    wa_text = (deliver_wa_text_en
               if client_lang == "en" and deliver_wa_text_en else deliver_wa_text)
    wa_builder = (deliver_wa_text_builder_en
                  if client_lang == "en" and deliver_wa_text_builder_en
                  else deliver_wa_text_builder)
    if wa_builder is not None:
        try:
            wa_text = wa_builder(answers) or wa_text
        except Exception as exc:
            log.warning("leadmagnet WA text builder error: %s", type(exc).__name__)
    # {consult} → бесплатная консультация. Ref агента тащим дальше: клиент пришёл
    # по его ссылке за чек-листом, и заявка со второго шага должна закрепиться за
    # ним же, а не потеряться в общем потоке.
    if wa_text and "{consult}" in wa_text:
        base = settings.WEBAPP_URL.rstrip("/")
        wa_text = wa_text.replace(
            "{consult}", f"{base}/consultation?ref={ref_slug}" if ref_slug
            else f"{base}/consultation")
    if wa_text:
        try:
            from app.wazzup import send_wa_text
            ok = send_wa_text(phone_norm, wa_text,
                              channel_id=settings.WAZZUP_CLIENT_CHANNEL_ID or None)
            log.info("client WA message → event=%s sent=%s", event_slug, ok)
        except Exception as exc:  # сеть/конфиг — не валим приём заявки
            log.warning("leadmagnet WA delivery error: %s", type(exc).__name__)

    # Карточка заявки в наш бот: владельцу и менеджерам (NOTIFY_TG_CHAT_IDS).
    # Внутренний канал, не сторонний сервис. error пробрасываем, чтобы в
    # сообщении был виден код сбоя api — иначе причину не с чем нести команде.
    _notify_admin_new_quiz(sub, partner, answers,
                           header=notify_header, question_titles=question_titles,
                           error=result.get("error"))
    log.info("quiz submit event=%s phone=%s agent=%s kommo=%s error=%s",
             event_slug or "consultation", _quiz_mask_phone(phone_norm),
             partner.id if partner else "-", result["status"], result.get("error"))
    return {"ok": True}


def partner_label(partner: Partner) -> str:
    """Как назвать партнёра в уведомлении и в CRM: имя → имя из Kommo → контакт → #id."""
    return (
        (partner.first_name or "").strip()
        or (partner.kommo_agent_name or "").strip()
        or (partner.phone or "").strip()
        or (partner.email or "").strip()
        or f"#{partner.id}"
    )


def _wa_digits(phone: str | None) -> str:
    """Цифры телефона для ссылки wa.me — чтобы менеджер писал клиенту в один тап."""
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _tg_send_lead(text: str) -> None:
    """Разослать карточку заявки владельцу и в рабочие чаты (2026-07-21).

    Получатели: ADMIN_TG_ID (как было) + NOTIFY_TG_CHAT_IDS — закрытая группа
    менеджеров. Пока api не создаёт сделки в Kommo, это единственный канал, по
    которому заявка доходит до менеджера, поэтому шлём каждому адресату отдельно
    и не даём сбою одного оборвать остальных. ПД в лог не пишем — только тип ошибки.
    """
    if not settings.BOT_TOKEN:
        return
    targets = [str(settings.ADMIN_TG_ID)] + [
        c for c in settings.NOTIFY_TG_CHAT_IDS if c != str(settings.ADMIN_TG_ID)
    ]
    for chat_id in targets:
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=10,
            )
            # Telegram отвечает HTTP 200 и при отказе («бот удалён из группы»,
            # «chat not found»), сообщая об этом в теле {"ok": false}. Без этой
            # проверки потеря заявки выглядит в логах как успех — а заявка сейчас
            # доходит до менеджера ТОЛЬКО так. description — техтекст Telegram,
            # ПД в нём нет, поэтому логируем целиком.
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if not body.get("ok"):
                log.error("lead notify REJECTED (chat=%s, http=%s): %s",
                          chat_id, r.status_code, body.get("description"))
            else:
                log.info("lead notify ok (chat=%s)", chat_id)
        except Exception as exc:
            log.error("lead notify failed (chat=%s): %s", chat_id, type(exc).__name__)


def _crm_status_line(status: str | None, kommo_lead_id: int | None,
                     error: str | None = None) -> str:
    """Строка о судьбе заявки в CRM — понятная менеджеру, а не только разработчику.

    Диагностика 21.07.2026: заявки принимаются, но api отвечает ошибкой и сделка
    в Kommo НЕ создаётся. Значит менеджеру нужно прямое указание «занести руками»,
    а не техническое слово «failed», которое легко пролистать.
    """
    if status == "sent" and kommo_lead_id:
        return f"✅ В CRM: сделка #{kommo_lead_id}"
    detail = f": {error}" if error else ""
    reason = "CRM отключена" if status == "dry" else f"сбой CRM{detail}"
    return f"⚠️ В CRM НЕ занесено — ВНЕСТИ ВРУЧНУЮ · {reason}"


def _notify_admin_new_quiz(sub: "QuizSubmission", partner: "Partner | None",
                           answers: dict, *, header: str, question_titles: dict,
                           error: str | None = None) -> None:
    """Карточка заявки владельцу и менеджерам. Best-effort: провал не влияет на
    приём (строка уже в БД). Шлём ПОЛНЫЙ телефон — это наш лид для звонка."""
    lines = [header, ""]
    lines.append(f"Имя: {sub.name or '—'}")
    lines.append(f"Телефон: +{sub.phone}")
    lines.append(f"Написать: https://wa.me/{sub.phone}")
    for qid, title in question_titles.items():
        if answers.get(qid):
            lines.append(f"• {title} — {answers[qid]}")
    lines.append("")
    if partner:
        lines.append(f"Агент: {partner.kommo_agent_name or partner.first_name or partner.ref_slug}")
    elif sub.ref_slug:
        lines.append(f"Реф-метка (агент не найден): {sub.ref_slug}")
    else:
        lines.append("Агент: — (пришёл не по партнёрской ссылке)")
    utm_src = sub.utm_source or sub.utm_campaign
    if utm_src:
        lines.append(f"UTM: source={sub.utm_source or '-'}, campaign={sub.utm_campaign or '-'}")
    lines.append("")
    lines.append(_crm_status_line(sub.kommo_status, sub.kommo_lead_id, error))
    _tg_send_lead("\n".join(lines))


@app.get("/join")
def join_partner_program() -> RedirectResponse:
    """Marketing-friendly URL: oncount-partners-production.up.railway.app/join
    → opens Telegram bot with the `partner` deep-link payload."""
    return RedirectResponse(
        f"https://t.me/{settings.BOT_USERNAME}?start=partner",
        status_code=302,
    )


INVITE_COOKIE = "invite_ref"


@app.get("/invite/{slug}")
def invite_link(slug: str, session: Session = Depends(get_session)) -> RedirectResponse:
    """Персональная инвайт-ссылка агента (Фаза 0.7). Кладёт ref_slug в cookie и ведёт
    на /login. На входе (TG/email) этот ref привяжет telegram_id/email к пред-созданному
    Partner-агенту — чтобы не плодить дубли. Неизвестный slug просто ведёт на /login."""
    resp = RedirectResponse("/login", status_code=302)
    exists = session.query(Partner).filter_by(ref_slug=slug).first() is not None
    if exists:
        resp.set_cookie(INVITE_COOKIE, slug, httponly=True, secure=True,
                        samesite="lax", max_age=3600)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if partner:
        return RedirectResponse("/dashboard", status_code=302)
    state = secrets.token_urlsafe(24)
    ref = request.cookies.get(INVITE_COOKIE)
    session.add(LoginSession(state=state, ref_slug=ref))
    session.commit()
    return templates.TemplateResponse("login.html", _ctx(request, None, state=state))


@app.get("/auth/bot-callback")
def auth_bot_callback(request: Request, state: str, next: str | None = None, session: Session = Depends(get_session)):
    """Завершение deep-link авторизации. Бот уже записал telegram_id для state.

    next — необязательная внутренняя страница, на которую кабинет откроется сразу
    после входа (например, курс-практикум). Разрешаем только локальные пути под
    /courses/, чтобы исключить open-redirect."""
    rec = session.get(LoginSession, state)
    expired = rec is not None and datetime.utcnow() - rec.created_at > LOGIN_SESSION_TTL
    # Ссылка одноразовая. Повторный тап по той же кнопке (частый кейс в Telegram
    # Web App — кнопка остаётся в чате) не должен пугать сырым JSON «already used»:
    # если в этой сессии уже есть кука — просто открываем кабинет; иначе показываем
    # аккуратную страницу со свежей рабочей кнопкой входа.
    if rec is None or rec.consumed_at is not None or expired:
        if current_partner(request, session):
            return RedirectResponse("/dashboard", status_code=302)
        return _relogin_notice(request, session)
    if rec.telegram_id is None:
        raise HTTPException(status.HTTP_425_TOO_EARLY, "Click the button inside the bot first")

    telegram_id = rec.telegram_id
    rec.consumed_at = datetime.utcnow()
    session.commit()

    partner = None
    # Фаза 0.7: вход по инвайт-ссылке → привязать telegram_id к пред-созданному
    # Partner-агенту. Привязываем ТОЛЬКО не активированного агента (status="invited")
    # и ТОЛЬКО если канал свободен — иначе чужой по той же ссылке перехватил бы
    # кабинет агента (security-review 2026-05-26, одноразовость инвайта).
    if rec.ref_slug:
        invited = session.query(Partner).filter_by(ref_slug=rec.ref_slug).first()
        if invited and invited.status == "invited" and invited.telegram_id is None:
            stray = session.query(Partner).filter_by(telegram_id=telegram_id).first()
            if stray and stray.id != invited.id:
                stray.telegram_id = None  # освободить уникальный telegram_id
                stray.status = "merged"
                session.flush()
            invited.telegram_id = telegram_id
            invited.status = "active"  # инвайт «погашен»: повторная привязка невозможна
            partner = invited

    if partner is None:
        partner = session.query(Partner).filter_by(telegram_id=telegram_id).first()
    if not partner:
        # бот к этому моменту уже создал партнёра в БД, но на всякий случай
        partner = Partner(
            telegram_id=telegram_id,
            ref_slug=generate_ref_slug(),
            status="pending",
        )
        session.add(partner)
        session.commit()
        session.refresh(partner)

    partner.last_login_at = datetime.utcnow()
    session.commit()

    token = issue_jwt(partner.id)
    dest = next if (next and next.startswith("/courses/")) else "/dashboard"
    response = RedirectResponse(dest, status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.JWT_TTL_DAYS * 86400,
    )
    return response


def _fresh_login_state(session: Session) -> str:
    """Свежий state для Telegram-кнопки на странице входа (как в login_page)."""
    state = secrets.token_urlsafe(24)
    session.add(LoginSession(state=state))
    session.commit()
    return state


def _relogin_notice(request: Request, session: Session) -> HTMLResponse:
    """Дружелюбная заглушка вместо сырого JSON, когда одноразовая ссылка входа
    уже использована/протухла, а активной сессии нет. Сразу даёт свежий deep-link
    в бота — один тап возвращает партнёра в кабинет."""
    state = _fresh_login_state(session)
    return templates.TemplateResponse(
        "relogin.html", _ctx(request, None, state=state)
    )


@app.post("/auth/email/request", response_class=HTMLResponse)
def auth_email_request(
    request: Request,
    email: str = Form(...),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Шаг 1 магической ссылки: принять email, отправить письмо со ссылкой входа.

    Анти-энумерация: ответ всегда одинаковый — «проверьте почту» — есть такой
    адрес или нет. Rate-limit: не больше EMAIL_RATE_LIMIT запросов на email за TTL.
    """
    email_norm = (email or "").strip().lower()
    lang = _lang(request)

    if EMAIL_RE.match(email_norm):
        window_start = datetime.utcnow() - EMAIL_TOKEN_TTL
        recent = (
            session.query(EmailLoginToken)
            .filter(
                EmailLoginToken.email == email_norm,
                EmailLoginToken.created_at >= window_start,
            )
            .count()
        )
        if recent < EMAIL_RATE_LIMIT:
            token = secrets.token_urlsafe(32)
            session.add(EmailLoginToken(
                token=token, email=email_norm,
                ref_slug=request.cookies.get(INVITE_COOKIE),
            ))
            session.commit()
            url = f"{settings.WEBAPP_URL}/auth/email/callback?token={token}"
            send_magic_link(email_norm, url, lang)

    return templates.TemplateResponse(
        "login.html",
        _ctx(request, None, state=_fresh_login_state(session), email_sent=True),
    )


@app.get("/auth/email/callback")
def auth_email_callback(token: str, session: Session = Depends(get_session)):
    """Шаг 2: клик по магической ссылке → выдаём JWT-cookie и заводим ЛК."""
    from sqlalchemy import func

    rec = session.get(EmailLoginToken, token)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Login link not found")
    if rec.consumed_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "Login link already used")
    if datetime.utcnow() - rec.created_at > EMAIL_TOKEN_TTL:
        raise HTTPException(status.HTTP_410_GONE, "Login link expired, request a new one")

    rec.consumed_at = datetime.utcnow()
    session.commit()

    partner = None
    # Фаза 0.7: вход по инвайт-ссылке → привязать email к Partner-агенту. Только
    # не активированного (status="invited") и со свободным email — защита от
    # перехвата кабинета по чужой ссылке (security-review 2026-05-26).
    if rec.ref_slug:
        invited = session.query(Partner).filter_by(ref_slug=rec.ref_slug).first()
        if invited and invited.status == "invited" and invited.email is None:
            stray = (
                session.query(Partner)
                .filter(func.lower(Partner.email) == rec.email)
                .first()
            )
            if stray and stray.id != invited.id:
                stray.email = None
                stray.status = "merged"
                session.flush()
            invited.email = rec.email
            invited.status = "active"  # инвайт «погашен»
            partner = invited

    if partner is None:
        partner = (
            session.query(Partner)
            .filter(func.lower(Partner.email) == rec.email)
            .first()
        )
    if not partner:
        # Новый партнёр без Telegram: заводим по email. first_name = локальная
        # часть адреса, чтобы плашка пользователя в шапке не была пустой.
        partner = Partner(
            email=rec.email,
            first_name=rec.email.split("@")[0],
            ref_slug=generate_ref_slug(),
            status="pending",
        )
        session.add(partner)
        try:
            session.commit()
            session.refresh(partner)
        except IntegrityError:
            # Гонка: партнёр с этим email появился между запросом и вставкой.
            session.rollback()
            partner = (
                session.query(Partner)
                .filter(func.lower(Partner.email) == rec.email)
                .first()
            )
            if partner is None:
                raise

    partner.last_login_at = datetime.utcnow()
    session.commit()

    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        issue_jwt(partner.id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.JWT_TTL_DAYS * 86400,
    )
    return response


def _wa_selfreg_allowed(request: Request) -> bool:
    """Можно ли сейчас отправить код на НЕИЗВЕСТНЫЙ номер (план 2026-08-07).

    Два независимых потолка, оба обязаны выполниться:
    · за сутки со всей платформы ушло меньше WA_SELFREG_DAILY_LIMIT кодов на
      неизвестные номера;
    · с этого IP за сутки было меньше WA_SELFREG_IP_DAILY_LIMIT таких запросов.

    Оба счётчика живут в памяти процесса: переживать рестарт им незачем — они режут
    массовую отправку в моменте, а не ведут учёт. Партнёрская платформа крутится
    одним процессом, так что общего хранилища для этого не нужно.
    """
    day_ago = datetime.utcnow() - timedelta(days=1)

    _wa_selfreg_day[:] = [t for t in _wa_selfreg_day if t >= day_ago]
    if len(_wa_selfreg_day) >= WA_SELFREG_DAILY_LIMIT:
        return False

    ip = (request.client.host if request.client else "") or "unknown"
    hits = [t for t in _wa_selfreg_ip_hits.get(ip, []) if t >= day_ago]
    if len(hits) >= WA_SELFREG_IP_DAILY_LIMIT:
        _wa_selfreg_ip_hits[ip] = hits
        return False

    now = datetime.utcnow()
    hits.append(now)
    _wa_selfreg_ip_hits[ip] = hits
    _wa_selfreg_day.append(now)
    return True


@app.post("/auth/phone/request", response_class=HTMLResponse)
def auth_phone_request(
    request: Request,
    phone: str = Form(...),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Шаг 1 входа по номеру: принять телефон → отправить 6-значный код в WhatsApp.

    Главный способ входа (телефон — сквозной идентификатор агента, план 2026-05-27).
    Анти-энумерация: ответ всегда одинаковый (показываем шаг ввода кода), есть
    такой агент в базе или нет. Код шлём ТОЛЬКО известному агенту (Partner.phone).
    Rate-limit: не больше PHONE_RATE_LIMIT кодов на номер за TTL.
    """
    norm = normalize_phone(phone)
    lang = _lang(request)

    if len(norm) >= PHONE_MIN_DIGITS:
        window_start = datetime.utcnow() - PHONE_CODE_TTL
        recent = (
            session.query(PhoneLoginToken)
            .filter(
                PhoneLoginToken.phone == norm,
                PhoneLoginToken.created_at >= window_start,
            )
            .count()
        )
        if recent < PHONE_RATE_LIMIT:
            # С 07.08.2026 (план 2026-08-07) код уходит и на НЕИЗВЕСТНЫЙ номер — так
            # работает самостоятельная регистрация партнёра по WhatsApp. Кабинет здесь
            # НЕ создаём: иначе каждый ввод случайного номера плодил бы мусорные Partner,
            # его заводит verify после подтверждения кода. Незнакомцу код уходит только
            # в пределах лимитов (_wa_selfreg_allowed).
            partner = find_partner_by_phone(session, norm)
            if partner is not None or _wa_selfreg_allowed(request):
                code = f"{secrets.randbelow(900_000) + 100_000}"  # 6 цифр, 100000–999999
                session.add(PhoneLoginToken(phone=norm, code_hash=hash_login_code(code)))
                session.commit()
                send_wa_code(norm, code, lang)

    return templates.TemplateResponse(
        "login.html",
        _ctx(request, None, state=_fresh_login_state(session),
             code_sent=True, code_phone=norm),
    )


@app.post("/auth/phone/verify", response_class=HTMLResponse)
def auth_phone_verify(
    request: Request,
    phone: str = Form(...),
    code: str = Form(...),
    session: Session = Depends(get_session),
):
    """Шаг 2 входа по номеру: проверить код → выдать JWT-cookie → кабинет агента.

    Брутфорс закрыт TTL (10 мин) + лимитом попыток (≤5) + rate-limit запросов кода
    + per-IP middleware. Все провалы (нет кода / просрочен / неверный / попытки
    кончились) дают ОДИН и тот же нейтральный ответ — иначе по тексту ошибки можно
    было бы отличить «номер в базе» от «номера нет» (анти-энумерация)."""
    norm = normalize_phone(phone)
    code = (code or "").strip()

    def reject() -> HTMLResponse:
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, None, state=_fresh_login_state(session),
                 code_sent=True, code_phone=norm, code_error=True),
        )

    rec = (
        session.query(PhoneLoginToken)
        .filter(
            PhoneLoginToken.phone == norm,
            PhoneLoginToken.consumed_at.is_(None),
        )
        .order_by(PhoneLoginToken.created_at.desc())
        .first()
    )
    if rec is None or datetime.utcnow() - rec.created_at > PHONE_CODE_TTL:
        return reject()
    if rec.attempts >= PHONE_CODE_MAX_ATTEMPTS:
        return reject()

    rec.attempts += 1
    session.commit()
    if not verify_login_code(code, rec.code_hash):
        return reject()

    rec.consumed_at = datetime.utcnow()
    session.commit()

    partner = find_partner_by_phone(session, norm)
    if partner is None:
        # Самостоятельная регистрация по WhatsApp (план 2026-08-07): код подтверждён,
        # значит номер принадлежит тому, кто его ввёл, — заводим кабинет здесь, а не
        # на шаге request, чтобы мусорные номера не оседали в базе. Статус active —
        # как у регистрации через бота (решение Николь 07.08): человек сразу видит
        # кабинет, а не заглушку «ждите подтверждения».
        partner = Partner(
            phone=norm,
            ref_slug=generate_ref_slug(),
            status="active",
            lang=_lang(request),
        )
        session.add(partner)
        session.commit()
        session.refresh(partner)
        session.add(PartnerIdentity(partner_id=partner.id, kind="phone", value=norm))
        session.commit()

    # Объединение каналов: телефон-Partner каноничен. Если у того же Kommo-агента
    # есть «осиротевшие» Partner из других каналов (бот/почта) — помечаем merged,
    # чтобы дедуп/дайджест считали их вытесненными (по аналогии с ref-привязкой).
    if partner.kommo_agent_enum_id is not None:
        strays = (
            session.query(Partner)
            .filter(
                Partner.kommo_agent_enum_id == partner.kommo_agent_enum_id,
                Partner.id != partner.id,
            )
            .all()
        )
        for stray in strays:
            stray.status = "merged"

    # Телефон больше не спрашиваем в онбординге: раз вошли по номеру — знаем его.
    # Проставляем в Partner.phone, если пуст (запасной канал WhatsApp-уведомлений).
    if not (partner.phone or "").strip():
        partner.phone = norm
    partner.last_login_at = datetime.utcnow()
    partner.status = "active"
    session.commit()

    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        issue_jwt(partner.id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.JWT_TTL_DAYS * 86400,
    )
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


# ─── Аккаунт: каналы входа (план 2026-06-02) ────────────────────────────────
# Один кабинет ↔ много каналов: Telegram (telegram_id) + номера (PartnerIdentity
# kind='phone'). Авторизованный агент добавляет ещё номер к СВОЕМУ кабинету —
# подтверждение кодом в WhatsApp. Так вход с Telegram и с WhatsApp ведёт в один
# и тот же кабинет (требование Николь 2026-06-02).
def _account_render(request: Request, session: Session, partner: Partner, **extra) -> HTMLResponse:
    phones = (session.query(PartnerIdentity)
              .filter_by(partner_id=partner.id, kind="phone").all())
    code = 400 if extra.pop("_bad", False) else 200
    return templates.TemplateResponse(
        "account.html", _ctx(request, partner, phones=phones, **extra), status_code=code)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    return _account_render(request, session, partner, code_sent=False, message=None)


@app.post("/account/phone/request", response_class=HTMLResponse)
def account_phone_request(request: Request, phone: str = Form(...),
                          session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    en = _lang(request) == "en"
    norm = normalize_phone(phone)
    if len(norm) < PHONE_MIN_DIGITS:
        return _account_render(request, session, partner, code_sent=False, _bad=True,
                               message=("The phone looks invalid." if en else "Номер выглядит некорректным."))
    # Код шлём, даже если номер сейчас висит на другом кабинете: подтверждение кода
    # = доказательство владения номером → на verify заберём его в текущий кабинет
    # (самолечение дублей; чужой номер не перехватить — код приходит владельцу).
    window = datetime.utcnow() - PHONE_CODE_TTL
    recent = (session.query(PhoneLoginToken)
              .filter(PhoneLoginToken.phone == norm, PhoneLoginToken.created_at >= window).count())
    if recent < PHONE_RATE_LIMIT:
        code = f"{secrets.randbelow(900_000) + 100_000}"
        session.add(PhoneLoginToken(phone=norm, code_hash=hash_login_code(code)))
        session.commit()
        send_wa_code(norm, code, _lang(request))
    return _account_render(request, session, partner, code_sent=True, code_phone=norm, message=None)


@app.post("/account/phone/verify", response_class=HTMLResponse)
def account_phone_verify(request: Request, phone: str = Form(...), code: str = Form(...),
                         session: Session = Depends(get_session)):
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    en = _lang(request) == "en"
    norm = normalize_phone(phone)
    code = (code or "").strip()
    bad = ("Code is invalid or expired." if en else "Код неверный или истёк.")
    rec = (session.query(PhoneLoginToken)
           .filter(PhoneLoginToken.phone == norm, PhoneLoginToken.consumed_at.is_(None))
           .order_by(PhoneLoginToken.created_at.desc()).first())
    if (rec is None or datetime.utcnow() - rec.created_at > PHONE_CODE_TTL
            or rec.attempts >= PHONE_CODE_MAX_ATTEMPTS):
        return _account_render(request, session, partner, code_sent=True, code_phone=norm, _bad=True, message=bad)
    rec.attempts += 1
    session.commit()
    if not verify_login_code(code, rec.code_hash):
        return _account_render(request, session, partner, code_sent=True, code_phone=norm, _bad=True, message=bad)
    rec.consumed_at = datetime.utcnow()
    session.commit()
    # Владение номером доказано кодом. Если номер уже есть как identity — забираем
    # его в текущий кабинет (перепривязка при дублях/старых тестах); иначе создаём.
    existing = session.query(PartnerIdentity).filter_by(kind="phone", value=norm).first()
    if existing is not None:
        existing.partner_id = partner.id
    else:
        session.add(PartnerIdentity(kind="phone", value=norm, partner_id=partner.id))
    if not (partner.phone or "").strip():
        partner.phone = norm
    session.commit()
    return RedirectResponse("/account", status_code=303)


# (slug, RU-подпись, EN-подпись) — шаблон онбординга выбирает подпись по lang.
SEGMENTS = [
    ("owner", "Владелец компании", "Company owner"),
    ("freelancer", "Фрилансер", "Freelancer"),
    ("employee", "Сотрудник компании", "Company employee"),
]


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    if partner.onboarded_at:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        "onboarding.html",
        _ctx(
            request, partner, segments=SEGMENTS,
            options=SURVEY_OPTIONS, labels=SURVEY_LABELS,
            answers=(partner.onboarding_answers or {}), message=None,
        ),
    )


@app.post("/onboarding", response_class=HTMLResponse)
def onboarding_submit(
    request: Request,
    segment: str = Form(...),
    sphere: str = Form(...),
    base_size: str = Form(...),
    sphere_other: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    # Онбординг = 3 вопроса (решение Николь 2026-07-24): роль + сфера + размер
    # базы, БЕЗ email (email берём позже — перед первой выплатой). Телефон уже
    # известен из входа по номеру. Сфера/база пишутся в тот же JSON-анкеты, что
    # и /onboarding-survey (общий белый список) — анкета их предзаполнит.
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)

    segment = (segment or "").strip().lower()
    sphere = (sphere or "").strip()
    base_size = (base_size or "").strip()
    en = _lang(request) == "en"

    def _reject(msg: str, code: int = 400) -> HTMLResponse:
        # Сохраняем введённое, чтобы форма не обнулилась при ошибке.
        draft = dict(partner.onboarding_answers or {})
        draft.update(segment=segment, sphere=sphere, base_size=base_size,
                     sphere_other=(sphere_other or "").strip())
        return templates.TemplateResponse(
            "onboarding.html",
            _ctx(
                request, partner, segments=SEGMENTS,
                options=SURVEY_OPTIONS, labels=SURVEY_LABELS,
                answers=draft, message=msg,
            ),
            status_code=code,
        )

    if segment not in {s[0] for s in SEGMENTS}:
        return _reject("Choose a role from the list." if en else "Выберите роль из списка.")
    if sphere not in _survey_values("sphere"):
        return _reject("Choose your field from the list." if en else "Выберите сферу из списка.")
    if base_size not in _survey_values("base_size"):
        return _reject("Choose your contact base size." if en else "Выберите размер базы контактов.")

    partner.segment = segment
    answers = dict(partner.onboarding_answers or {})
    answers["sphere"] = sphere
    answers["base_size"] = base_size
    # Свободный текст «Другое» — только к варианту other; иначе подчищаем.
    if sphere == "other" and (sphere_other or "").strip():
        answers["sphere_other"] = (sphere_other or "").strip()[:SURVEY_OTHER_MAXLEN]
    else:
        answers.pop("sphere_other", None)
    partner.onboarding_answers = answers
    partner.onboarded_at = datetime.utcnow()
    session.commit()
    # Сразу к деньгам: вкладка «Пост» на дашборде — креатив мастер-класса с
    # личной ссылкой (решение Николь 2026-07-24). Полную анкету не форсируем:
    # остаётся мягким баннером, sphere/base_size в ней уже предзаполнены.
    return RedirectResponse("/dashboard#social", status_code=302)


# ─── Анкета партнёра (Фаза L) ───────────────────────────────────────────────
# МЯГКАЯ форма: НЕ блокирует кабинет. Отдельный маршрут /onboarding-survey
# (НЕ трогаем блокирующий /onboarding, который собирает базовый контакт).
SURVEY_SNOOZE_COOKIE = "survey_snooze"  # «Позже»: прячет баннер на время


@app.get("/onboarding-survey", response_class=HTMLResponse)
def onboarding_survey_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    info = partner_onboarding(partner, _lang(request))
    return templates.TemplateResponse(
        "onboarding_survey.html",
        _ctx(
            request, partner,
            options=SURVEY_OPTIONS,
            labels=SURVEY_LABELS,
            answers=info["answers"],   # предзаполнение при повторном входе
            completed=info["completed"],
            survey_draft=SURVEY_DRAFT,
            message=None,
        ),
    )


@app.post("/onboarding-survey", response_class=HTMLResponse)
def onboarding_survey_submit(
    request: Request,
    sphere: str = Form(""),
    sphere_other: str = Form(""),
    uae_experience: str = Form(""),
    b2b_flow: str = Form(""),
    b2b_volume: str = Form(""),
    base_size: str = Form(""),
    social_channels: list[str] = Form(default=[]),
    social_audience: str = Form(""),
    payout_method: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    en = _lang(request) == "en"

    def clean_other(v: str) -> str:
        return (v or "").strip()[:SURVEY_OTHER_MAXLEN]

    # Сборка ответов СТРОГО по белым спискам (всё вне списка отбрасывается).
    answers: dict = {}
    single = {
        "sphere": sphere, "uae_experience": uae_experience, "b2b_flow": b2b_flow,
        "base_size": base_size, "social_audience": social_audience,
        "payout_method": payout_method,
    }
    for field, val in single.items():
        val = (val or "").strip()
        if val and val in _survey_values(field):
            answers[field] = val
    # b2b_volume — только если поток есть (не "none").
    bv = (b2b_volume or "").strip()
    if answers.get("b2b_flow") in ("steady", "occasional") and bv in _survey_values("b2b_volume"):
        answers["b2b_volume"] = bv
    # Соцсети — мультивыбор; фильтруем по белому списку, режем дубли, порядок храним.
    allowed_ch = _survey_values("social_channels")
    chans = [c.strip() for c in social_channels if c and c.strip() in allowed_ch]
    seen: set[str] = set()
    chans = [c for c in chans if not (c in seen or seen.add(c))]
    # «Нет соцсетей» несовместимо с реальными каналами: если выбрано и то и то,
    # реальные каналы важнее — убираем "none" (иначе менеджер видит противоречие).
    if "none" in chans and len(chans) > 1:
        chans = [c for c in chans if c != "none"]
    if chans:
        answers["social_channels"] = chans
    # Свободный текст «other» — только если выбран соответствующий вариант.
    if answers.get("sphere") == "other":
        txt = clean_other(sphere_other)
        if txt:
            answers["sphere_other"] = txt

    # Серверная валидация обязательных вопросов.
    missing = [f for f in SURVEY_REQUIRED if f not in answers]
    if missing:
        msg = ("Please answer the required questions (marked *)."
               if en else "Пожалуйста, ответьте на обязательные вопросы (со звёздочкой *).")
        return templates.TemplateResponse(
            "onboarding_survey.html",
            _ctx(
                request, partner,
                options=SURVEY_OPTIONS, labels=SURVEY_LABELS,
                answers=answers,  # сохраняем введённое для повторного показа
                completed=partner.survey_completed_at is not None,
                survey_draft=SURVEY_DRAFT, message=msg,
            ),
            status_code=400,
        )

    partner.onboarding_answers = answers
    partner.survey_completed_at = datetime.utcnow()
    session.commit()
    # После заполнения снуз больше не нужен — баннер и так скрыт по completed.
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.delete_cookie(SURVEY_SNOOZE_COOKIE)
    return resp


@app.post("/onboarding-survey/later")
def onboarding_survey_later(request: Request, session: Session = Depends(get_session)):
    """«Позже»: мягко прячет баннер-приглашение на неделю (cookie, БЕЗ записи в
    БД и без блокировки кабинета). Анкета остаётся доступной из шапки/ссылки."""
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        SURVEY_SNOOZE_COOKIE, "1",
        max_age=7 * 24 * 3600, httponly=True, secure=True, samesite="lax",
    )
    return resp


@app.post("/checklist/dismiss")
def checklist_dismiss(request: Request, session: Session = Depends(get_session)):
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    partner.checklist_dismissed_at = datetime.utcnow()
    session.commit()
    return RedirectResponse("/dashboard", status_code=302)


def _leads_items(session: Session, partner: Partner) -> list[dict]:
    """Единый список заявок партнёра для таблицы (шаблон _leads_table.html).

    Обычные лиды + строки дохода 2-го уровня (за суб-агентов) идут вперемешку по
    дате, а не блоком в конце (решение Николь 2026-07-21). Тип помечаем ключом
    "kind". Дата 2-го уровня приходит строкой "дд.мм.гггг" — парсим для
    сортировки, при кривом формате роняем в самый низ, а не падаем.
    """
    leads = (
        session.query(Lead)
        .filter_by(partner_id=partner.id)
        .order_by(Lead.created_at.desc())
        .limit(100)
        .all()
    )

    def _l2_date(s: str):
        try:
            return datetime.strptime((s or "").strip(), "%d.%m.%Y")
        except (ValueError, TypeError):
            return datetime.min

    items = [{"kind": "lead", "lead": l, "sort_dt": l.created_at} for l in leads]
    for it in (partner.l2_income or []):
        items.append({"kind": "l2", "l2": it, "sort_dt": _l2_date(it.get("date"))})
    items.sort(key=lambda x: x["sort_dt"] or datetime.min, reverse=True)
    return items


def _tools_ctx(session: Session, partner: Partner, request: Request) -> dict:
    """Контекст раздела «Тексты и ссылки» (шаблон _tools_content.html).

    Вкладки = СПОСОБЫ привлечения (METHODS). В каждом тексте уже вшита
    персональная ссылка партнёра (плейсхолдер {link} → links[link_key]).
    До 2026-07-21 собирался в маршруте /tools; теперь раздел живёт на дашборде.
    """
    # База — канонический домен из WEBAPP_URL, а НЕ request.base_url. Иначе агент,
    # зашедший по старой закладке (railway-адрес), копировал бы клиентам ссылки на
    # старый домен, а схема бралась бы как http:// (за прокси Railway приложение не
    # видит X-Forwarded-Proto). С 2026-07-27 канон — https://www.oncount.co.
    links = _personal_links(partner.ref_slug, settings.WEBAPP_URL.rstrip("/"),
                            lang=_lang(request))
    # Все активные тексты с непустым method, сгруппированы по способу. Порядок
    # внутри способа — order_index, id. Тексты без method (NULL) не показываем.
    rows = (
        session.query(MessageTemplate)
        .filter(MessageTemplate.is_active.is_(True))
        .filter(MessageTemplate.method.isnot(None))
        .order_by(MessageTemplate.method, MessageTemplate.order_index, MessageTemplate.id)
        .all()
    )
    method_groups: dict[str, list] = {}
    for it in rows:
        method_groups.setdefault(it.method, []).append(it)
    # Контакт менеджера для CTA вкладки «События»: подтверждённый WhatsApp из
    # PARTNER_MANAGER (единый источник), fallback — общий контакт.
    manager_wa = next(
        (c["value"] for c in PARTNER_MANAGER["contacts"]
         if c["channel"] == "whatsapp" and c.get("confirmed")),
        settings.CONTACT_WA_NUMBER,
    )
    return {
        "ref_slug": partner.ref_slug,
        "methods_order": METHODS_ORDER,
        "method_default": METHODS_DEFAULT,
        "method_groups": method_groups,
        "links": links,
        "manager_wa": manager_wa,
        "accountants": ACCOUNTANTS,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    if not partner.onboarded_at:
        return RedirectResponse("/onboarding", status_code=302)

    leads_q = session.query(Lead).filter_by(partner_id=partner.id)
    leads_count = leads_q.count()
    successful = leads_q.filter(Lead.status == "won").count()
    in_progress = leads_q.filter(Lead.status.in_(["new", "in_progress"])).count()
    rejected = leads_q.filter(Lead.status == "lost").count()
    conversion = round(successful / leads_count * 100, 1) if leads_count else 0.0

    won_rows = leads_q.filter(Lead.status == "won").all()
    # «Заработано» — комиссия партнёра, не сумма чеков клиентов (решение Николь
    # 2026-07-21). NULL-комиссии дают 0. Плюс доход 2-го уровня (за суб-агентов).
    # float() на каждом слагаемом — см. комментарий в _balance_kpi: Decimal из
    # Postgres + float из l2_total давали TypeError и 500 на /dashboard.
    earned_total = (sum(float(getattr(l, "commission_aed", None) or 0) for l in won_rows)
                    + float(l2_total(partner)))
    # 3 шага (решение Николь 2026-07-23, вместо прежних 4): ссылка+текст клиенту →
    # 2 видео → или передать контакт клиента (менеджер сам ведёт и переводит
    # вознаграждение). Шаги 1 и 3 — два альтернативных пути привести клиента.
    checklist_steps = [
        {
            "label": "Скопируйте свою партнёрскую ссылку и готовый текст — отправьте их вашему клиенту",
            "label_en": "Copy your partner link and a ready-made message — send them to your client",
            "done": partner.links_viewed_at is not None,
            "href": "/tools#intro",  # → редирект на /dashboard#intro, блок раскроется
        },
        {
            "label": "Посмотрите 2 видео: об условиях сотрудничества и кабинете партнёра",
            "label_en": "Watch 2 videos: partnership terms & partner dashboard",
            "done": partner.courses_viewed_at is not None,
            "href": "/courses",
        },
        {
            "label": "Или просто передайте контакт клиента — менеджер сам свяжется, всё оформит и переведёт вам вознаграждение",
            "label_en": "Or simply pass your client's contact — the manager will reach out, handle everything and transfer your reward",
            "done": leads_count > 0,
            "href": "/transfer",
        },
    ]
    show_checklist = (
        partner.checklist_dismissed_at is None
        and not all(s["done"] for s in checklist_steps)
    )
    # Баннер-приглашение пройти анкету (Фаза L). Мягкий: виден, пока анкета не
    # пройдена И партнёр не нажал «Позже» (cookie-снуз). Не блокирует кабинет.
    show_survey_banner = (
        partner.survey_completed_at is None
        and request.cookies.get(SURVEY_SNOOZE_COOKIE) != "1"
    )

    # FAQ перенесён в самый низ дашборда (раньше — отдельная страница /faq).
    faq_items = (
        session.query(FaqItem)
        .filter_by(is_active=True)
        .order_by(FaqItem.category, FaqItem.order_index)
        .all()
    )
    faq_categories: dict[str, list[FaqItem]] = {}
    for item in faq_items:
        faq_categories.setdefault(item.category, []).append(item)

    # ── Блок «Сообщество» — КУРАТОРСКАЯ соц-витрина: фиксированные цифры и имена,
    # утверждённые Николь (это НЕ живые данные из БД). Цель — соц-доказательство
    # масштаба партнёрской сети. В топе — только имя, по убыванию числа контактов.
    community = {
        "partners": 167,
        "total_contacts": 567,
        "top": [
            {"name": "Евгений", "name_en": "Evgeny", "count": 45},
            {"name": "Ильяс", "name_en": "Ilyas", "count": 28},
            {"name": "Даниил", "name_en": "Daniil", "count": 21},
            {"name": "Мари", "name_en": "Mari", "count": 16},
            {"name": "Ольга", "name_en": "Olga", "count": 12},
        ],
    }

    # Каналы входа в этот кабинет (план 2026-06-02): Telegram + подтверждённые
    # номера. Показываем прямо на дашборде (вместо отдельного пункта меню).
    account_phones = (
        session.query(PartnerIdentity)
        .filter_by(partner_id=partner.id, kind="phone")
        .all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request,
            partner,
            account_phones=account_phones,
            kpi={
                "conversion": conversion,
                "leads": leads_count,
                "successful": successful,
                "rejected": rejected,
                "in_progress": in_progress,
                # «Заработано» — комиссия партнёра, не оборот (решение Николь
                # 2026-07-21). См. _balance_kpi.
                "earned_aed": float(earned_total),
                # Ожидаемая комиссия: $300 (мин) … $1000 (средн) — только по
                # АКТИВНЫМ заявкам (new/in_progress), без отказов и оплаченных
                # (решение Николь 2026-07-21). in_progress здесь уже new+in_progress.
                "active_leads": in_progress,
                "expected_usd_low": in_progress * 300,
                "expected_usd_high": in_progress * 1000,
                # Средняя комиссия — серый ориентир в столбце «Ваша комиссия»
                # (_leads_table.html). Балансовую полосу НЕ трогает.
                "avg_commission_aed": float(AVG_COMMISSION_AED),
            },
            checklist_steps=checklist_steps,
            show_checklist=show_checklist,
            show_survey_banner=show_survey_banner,
            faq_categories=faq_categories,
            community=community,
            # Таблица заявок переехала на дашборд (решение Николь 2026-07-21),
            # отдельной страницы /leads больше нет — она редиректит сюда.
            items=_leads_items(session, partner),
            # Калькулятор вознаграждения (#calc, решение Николь 2026-07-23):
            # тарифы и разовые услуги под язык интерфейса. Считает браузер,
            # сервер только отдаёт цифры — см. app/calc_config.py.
            calc=calc_data(_lang(request)),
            # То же с «Текстами и ссылками» (свёрнутый блок под чек-листом):
            # контекст вкладок собираем здесь, шаблон — _tools_content.html.
            **_tools_ctx(session, partner, request),
        ),
    )


@app.get("/leads", response_class=HTMLResponse)
def leads(request: Request) -> RedirectResponse:
    """LEGACY: страница «Все заявки» переехала на дашборд (решение Николь
    2026-07-21). Адрес живёт редиректом — на него ведут закладки партнёров,
    ссылки в текстах кабинета и старые уведомления."""
    return RedirectResponse("/dashboard#leads", status_code=302)


CONSULT_TEXT_TPL = (
    "Здравствуйте! Хочу записаться на бесплатную консультацию "
    "с бухгалтером ONCOUNT. Код партнёра: {slug}"
)
MCLASS_TEXT_TPL = (
    "Здравствуйте! Хочу попасть на мастер-класс с бухгалтером ONCOUNT. "
    "Код партнёра: {slug}"
)

# Невидимая метка в WA/TG-сообщении: код партнёра, закодированный
# zero-width Unicode. Переживает удаление видимого «Код партнёра: …».
# Парсер на стороне inbox-intercom-backend (см. encode_slug_invisible ниже):
#   1) найти подстроку между ZW_START и ZW_END
#   2) каждый ZW_ZERO → '0', ZW_ONE → '1'
#   3) собрать байты по 8 бит, ord→chr → slug
ZW_ZERO = "​"   # zero-width space
ZW_ONE = "‌"    # zero-width non-joiner
ZW_START = "‍"  # zero-width joiner
ZW_END = "⁠"    # word joiner


def encode_slug_invisible(slug: str) -> str:
    bits = "".join(format(ord(c), "08b") for c in slug)
    body = "".join(ZW_ZERO if b == "0" else ZW_ONE for b in bits)
    return ZW_START + body + ZW_END


def _build_text(template: str, slug: str) -> str:
    return template.format(slug=slug) + encode_slug_invisible(slug)


def _redirect_to_chat(channel: str, text: str) -> RedirectResponse:
    encoded = quote(text)
    if channel == "tg":
        url = f"https://t.me/{settings.CONTACT_TG_USERNAME}?text={encoded}"
    elif channel == "wa":
        url = f"https://wa.me/{settings.CONTACT_WA_NUMBER}?text={encoded}"
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown channel")
    return RedirectResponse(url, status_code=302)


@app.get("/ct/{slug}")
def short_consult_tg(slug: str, request: Request) -> RedirectResponse:
    linkstat.record_click("consultation", "tg", slug, request.headers.get("user-agent"))
    return _redirect_to_chat("tg", _build_text(CONSULT_TEXT_TPL, slug))


@app.get("/cw/{slug}")
def short_consult_wa(slug: str, request: Request) -> RedirectResponse:
    linkstat.record_click("consultation", "wa", slug, request.headers.get("user-agent"))
    return _redirect_to_chat("wa", _build_text(CONSULT_TEXT_TPL, slug))


@app.get("/mt/{slug}")
def short_mclass_tg(slug: str, request: Request) -> RedirectResponse:
    linkstat.record_click("mk", "tg", slug, request.headers.get("user-agent"))
    return _redirect_to_chat("tg", _build_text(MCLASS_TEXT_TPL, slug))


@app.get("/mw/{slug}")
def short_mclass_wa(slug: str, request: Request) -> RedirectResponse:
    linkstat.record_click("mk", "wa", slug, request.headers.get("user-agent"))
    return _redirect_to_chat("wa", _build_text(MCLASS_TEXT_TPL, slug))


@app.get("/p/{slug}")
def short_partner_bot(slug: str, request: Request) -> RedirectResponse:
    linkstat.record_click("partner_bot", "bot", slug, request.headers.get("user-agent"))
    return RedirectResponse(
        f"https://t.me/{settings.BOT_USERNAME}?start=ref_{slug}",
        status_code=302,
    )


@app.get("/tools")
def tools(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    """LEGACY: раздел «Тексты и ссылки» переехал на дашборд свёрнутым блоком
    (решение Николь 2026-07-21). Адрес живёт редиректом — на него ведут шаг 1
    чек-листа, кнопки бота и закладки партнёров. Заход сюда по-прежнему
    отмечает «скопируй ссылку» выполненным: смысл шага не изменился.
    Глубокие якоря (#intro/#broadcast/#social/#leadmagnet и старые
    #links/#messages/#kits/#event) разворачивают блок и открывают нужную вкладку —
    это делает скрипт в _tools_content.html.
    """
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    if partner.links_viewed_at is None:
        partner.links_viewed_at = datetime.utcnow()
        session.commit()
    anchor = request.url.fragment or "tools"
    return RedirectResponse(f"/dashboard#{anchor}", status_code=302)


# Старые URL → объединённая страница (бот /links, /messages и закладки живут).
# Якоря переехали на способы (план 2026-06-02): ссылки теперь во вкладке intro.
@app.get("/links")
def links_redirect() -> RedirectResponse:
    return RedirectResponse("/tools#intro", status_code=302)


@app.get("/transfer", response_class=HTMLResponse)
def transfer_get(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("transfer.html", _ctx(request, partner, message=None))


@app.post("/transfer", response_class=HTMLResponse)
def transfer_post(
    request: Request,
    client_name: str = Form(...),
    client_phone: str = Form(...),
    task_description: str = Form(...),
    do_not_offer: str = Form(...),
    client_telegram: str = Form(""),
    company_name: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)

    client_name_v = client_name.strip()
    client_phone_v = client_phone.strip()
    task_v = task_description.strip()
    do_not_offer_v = do_not_offer.strip()
    client_telegram_v = client_telegram.strip()
    company_name_v = company_name.strip()

    lead = Lead(
        partner_id=partner.id,
        client_name=client_name_v,
        client_phone=client_phone_v,
        client_telegram=client_telegram_v or None,
        company_name=company_name_v or None,
        task_description=task_v,
        do_not_offer=do_not_offer_v,
        status="new",
    )
    session.add(lead)
    session.commit()

    # Карточка клиента владельцу и менеджерам (NOTIFY_TG_CHAT_IDS). Сделку в Kommo
    # заводит менеджер руками — партнёр-сервис в CRM не пишет, а api сейчас на
    # заявках отвечает ошибкой (диагностика 21.07.2026). Поэтому это сообщение —
    # единственный путь заявки к менеджеру, и в нём должно быть всё для звонка.
    # Лид уже в БД: если TG упал, ничего не теряем — есть /leads и /admin/transfers.
    # ПД клиента летят во внутренний канал (свой бот → свои чаты), как уже делает
    # app/bot.py для ТГ-флоу.
    lines = [
        "🆕 Новый клиент от партнёра",
        "",
        f"Партнёр: {partner_label(partner)}",
        f"Имя клиента: {client_name_v}",
        f"Телефон: {client_phone_v}",
        f"Написать: https://wa.me/{_wa_digits(client_phone_v)}",
        f"Услуга: {task_v}",
        f"Что НЕ предлагать: {do_not_offer_v}",
    ]
    if client_telegram_v:
        lines.append(f"Telegram клиента: {client_telegram_v}")
    if company_name_v:
        lines.append(f"Компания: {company_name_v}")
    lines.append("")
    lines.append("⚠️ В CRM НЕ занесено — ВНЕСТИ ВРУЧНУЮ (воронка 1.1, «новый лид»)")
    _tg_send_lead("\n".join(lines))

    msg = (
        "Client referred. A manager will be in touch within an hour during business hours (9:00–18:00 Dubai time)."
        if _lang(request) == "en"
        else "Клиент передан. Менеджер свяжется в течение часа в рабочее время с 9-18.00 дубай."
    )
    return templates.TemplateResponse(
        "transfer.html",
        _ctx(request, partner, message=msg),
    )


@app.get("/products", response_class=HTMLResponse)
@app.get("/kb/products", response_class=HTMLResponse)
def products(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    if partner.products_viewed_at is None:
        partner.products_viewed_at = datetime.utcnow()
        session.commit()
    items = (
        session.query(ProductBlock)
        .filter_by(is_active=True)
        .order_by(ProductBlock.order_index)
        .all()
    )
    return templates.TemplateResponse(
        "products.html",
        _ctx(request, partner, items=items, kpi=_balance_kpi(session, partner)),
    )


# Тексты рассылок и партнёрский кит объединены в /tools (вкладки). Старые URL
# редиректят на соответствующий якорь — bot.py /messages и закладки не ломаем.
@app.get("/messages")
def messages_redirect() -> RedirectResponse:
    return RedirectResponse("/tools#broadcast", status_code=302)


@app.get("/kits")
def kits_redirect() -> RedirectResponse:
    return RedirectResponse("/tools#intro", status_code=302)


# FAQ переехал в самый низ дашборда. Старые ссылки (/faq, /kb/faq, футер бота)
# редиректим на якорь #faq, чтобы ничего не сломалось.
@app.get("/faq")
@app.get("/kb/faq")
def faq() -> RedirectResponse:
    return RedirectResponse("/dashboard#faq", status_code=302)


@app.get("/courses", response_class=HTMLResponse)
@app.get("/kb/courses", response_class=HTMLResponse)
def courses(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    if partner.courses_viewed_at is None:
        partner.courses_viewed_at = datetime.utcnow()
        session.commit()
    items = (
        session.query(Course)
        .filter_by(is_active=True)
        .order_by(Course.order_index)
        .all()
    )
    mastermind_details = [
        p.strip() for p in settings.MASTERMIND_DETAILS.split(";") if p.strip()
    ]
    mastermind_details_en = [
        p.strip() for p in settings.MASTERMIND_DETAILS_EN.split(";") if p.strip()
    ]
    return templates.TemplateResponse(
        "courses.html",
        _ctx(
            request,
            partner,
            items=items,
            mastermind_title=settings.MASTERMIND_TITLE,
            mastermind_details=mastermind_details,
            mastermind_footer=settings.MASTERMIND_FOOTER,
            mastermind_title_en=settings.MASTERMIND_TITLE_EN,
            mastermind_details_en=mastermind_details_en,
            mastermind_footer_en=settings.MASTERMIND_FOOTER_EN,
            # Стрелка ведёт на будущую страницу программы Mastermind (пока заглушка).
            mastermind_url="#",
            training_video_program_id=settings.TRAINING_VIDEO_PROGRAM_ID,
            training_video_cabinet_id=settings.TRAINING_VIDEO_CABINET_ID,
        ),
    )


# Богатые страницы уроков курса. Контент — редакторский (этапы, промпты, тайм-коды),
# поэтому живёт в отдельных Jinja-шаблонах, а не в БД (гибрид, план
# 2026-05-22-kabinet-kursy-uroki-i18n). Маппинг (slug, день) → шаблон. Незнакомая пара
# → редирект на витрину, чтобы не плодить 404 на «локед»/будущих днях.
LESSON_TEMPLATES: dict[tuple[str, int], str] = {
    ("ai-employees-setup", 1): "course-ai-setup-day1.html",
    ("ai-employees-setup", 2): "course-ai-setup-day2.html",
}


@app.get("/courses/{slug}", response_class=HTMLResponse)
def course_entry(slug: str, request: Request, session: Session = Depends(get_session)):
    """CTA «Начать/Продолжить» ведёт сюда → редирект на первый день, если у курса
    есть уроки; иначе обратно на витрину (slug в шаблоне не хардкодим — решает роут)."""
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    if any(s == slug for (s, _d) in LESSON_TEMPLATES):
        return RedirectResponse(f"/courses/{slug}/day/1", status_code=302)
    return RedirectResponse("/courses", status_code=302)


@app.get("/courses/{slug}/day/{day}", response_class=HTMLResponse)
def course_lesson(
    slug: str, day: int, request: Request, session: Session = Depends(get_session)
):
    partner = current_partner(request, session)
    if not partner:
        return RedirectResponse("/login", status_code=302)
    template = LESSON_TEMPLATES.get((slug, day))
    if not template:
        return RedirectResponse("/courses", status_code=302)
    return templates.TemplateResponse(template, _ctx(request, partner))
