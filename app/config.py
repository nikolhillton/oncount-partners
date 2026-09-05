import os
from dotenv import load_dotenv

load_dotenv()


_JWT_SECRET = os.getenv("JWT_SECRET", "")
if not _JWT_SECRET or _JWT_SECRET == "dev-secret-change-me":
    raise RuntimeError(
        "JWT_SECRET env var must be set to a non-default value. "
        "Set it in Railway → Variables before deploying."
    )


class Settings:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "community_oncount_bot")
    WEBAPP_URL: str = os.getenv("WEBAPP_URL", "http://localhost:8000")
    JWT_SECRET: str = _JWT_SECRET
    JWT_ALGO: str = "HS256"
    JWT_TTL_DAYS: int = 30
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://oncount:oncount@localhost:5432/oncount_partners",
    )
    # Resend — транзакционные письма (магическая ссылка входа по email).
    # Пустой RESEND_API_KEY → dev-режим: ссылка пишется в лог, письмо не уходит.
    # EMAIL_FROM должен быть на верифицированном в Resend домене (nikole-ai.com),
    # иначе Resend отклоняет отправку. Имя отправителя — ONCOUNT (бренд).
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "ONCOUNT <noreply@nikole-ai.com>")
    # Централизация Kommo (2026-06-03): партнёр-сервис обращается к Kommo ТОЛЬКО
    # через наш api-сервис (NestJS), префикс /api/partner/* под ключом x-api-key.
    # Глобальный префикс /api добавляется в путях клиента (см. app/api_client.py).
    # Пусто → dev-режим: синк Kommo не регистрируется, лиды квиза остаются 'dry'.
    ONCOUNT_API_URL: str = os.getenv("ONCOUNT_API_URL", "")
    # Ключ к /api/partner/* (выдан api). Пусто в dev. Сетевой рубеж — Security Group
    # на EC2 (см. Documentation/PARTNER_API_SECURITY_DESIGN.md §4).
    PARTNER_API_KEY: str = os.getenv("PARTNER_API_KEY", "")
    # ─── Бот оплат интенсива @Nikol_hilton_bot (план 2026-08-03) ───────────────
    # Отдельный токен: у @community_oncount_bot свой getUpdates, два бота в одном
    # процессе не конфликтуют, пока у каждого свой токен и диспетчер.
    # Пусто → бот не поднимается, остальная платформа работает как раньше.
    PAY_BOT_TOKEN: str = os.getenv("PAY_BOT_TOKEN", "")
    # Username того же бота — из него собираются кнопки «купить» на лендинге
    # (`t.me/<username>?start=pay`). Отдельно от токена: username нужен вёрстке,
    # а токен — секрет, которому в шаблоне делать нечего.
    # ⚠️ «hilton» с одной «l»: у личного аккаунта Николь — с двумя.
    PAY_BOT_USERNAME: str = os.getenv("PAY_BOT_USERNAME", "Nikol_hilton_bot")
    # Ключ кассы Lava: выставление счетов и проверка оплаты (app/lava.py).
    # Пусто → бот принимает людей, но оплату подтверждает человек.
    LAVA_API_KEY: str = os.getenv("LAVA_API_KEY", "")
    # Чат участников интенсива. Бот узнаёт его САМ, когда Николь добавит его в
    # группу (событие my_chat_member → таблица bot_settings). Переменная — только
    # для случая «хотим задать вручную»; пусто = берём из bot_settings.
    INTENSIVE_CHAT_ID: str = os.getenv("INTENSIVE_CHAT_ID", "")
    # Закрытый канал Николь (план 2026-08-03). Тот же приём, что и с чатом выше:
    # бот узнаёт канал сам из my_chat_member, когда его сделают админом.
    # Переменная — ручное переопределение; пусто = берём из bot_settings.
    NIKOL_CHANNEL_ID: str = os.getenv("NIKOL_CHANNEL_ID", "")
    # Ключ к счётчикам по меткам рассылки (`GET /admin/api/channel-tags`, план
    # 2026-09-04). Их забирает по расписанию ARDORIUM — у машины нет браузера и
    # cookie владельца, поэтому вторая дверь: заголовок `X-Api-Token`.
    # ⚠️ Пусто → токенная дверь ЗАКРЫТА совсем, остаётся только вход по cookie.
    # Иначе незаполненная переменная в Railway открыла бы адрес любому, кто
    # пришлёт пустой заголовок. Наружу уезжают только метки и числа.
    CHANNEL_TAGS_TOKEN: str = os.getenv("CHANNEL_TAGS_TOKEN", "")
    # ─── Платный клуб (план 2026-08-04) ───────────────────────────────────────
    # Канал клуба. Как и выше, бот узнаёт его сам из my_chat_member; переменная —
    # ручное переопределение. ⚠️ У клуба и у канала 18+ РАЗНЫЕ id: перепутать их
    # значит выдать доступ не туда, поэтому клубный id живёт своим ключом.
    # Значение по умолчанию — реальный канал клуба (тот же приём, что у
    # ADMIN_TG_ID). Без него бот не смог бы отличить клубный канал от канала
    # 18+: событие «бота сделали админом» у них одно на двоих, и без заранее
    # известного id клубные ссылки ушли бы не туда. Переменная в Railway
    # переопределяет — например, чтобы проверить поток на тестовом канале.
    CLUB_CHANNEL_ID: str = os.getenv("CLUB_CHANNEL_ID", "-1004431954242")
    # Kill-switch цепочки удержания: "0" гасит напоминания и удаления, приём
    # оплат и выдача доступа продолжают работать. Правило плана: сомневаешься —
    # гаси цикл, а не приём денег.
    CLUB_RETENTION_LIVE: bool = os.getenv("CLUB_RETENTION_LIVE", "1") not in ("0", "false", "")
    # Предохранитель от массового выноса из-за сбоя API: больше стольких удалений
    # за сутки цикл не делает, а зовёт Николь.
    CLUB_MAX_REMOVALS_PER_DAY: int = int(os.getenv("CLUB_MAX_REMOVALS_PER_DAY", "5"))
    # Wazzup24 — доставка кода входа / уведомлений в WhatsApp. ПОКА напрямую (план:
    # следующий PR переведёт отправку на api /api/partner/notify). Пустой ключ/канал
    # → dev-режим: в сеть ничего не уходит (см. app/wazzup.py).
    # WAZZUP_TEST_ONLY_NUMBER — предохранитель теста: если задан, шлём ТОЛЬКО на него.
    WAZZUP_API_KEY: str = os.getenv("WAZZUP_API_KEY", "")
    WAZZUP_CHANNEL_ID: str = os.getenv("WAZZUP_CHANNEL_ID", "")
    WAZZUP_TEST_ONLY_NUMBER: str = os.getenv("WAZZUP_TEST_ONLY_NUMBER", "")
    # Канал для КЛИЕНТСКИХ сообщений (подтверждения /mk и /consultation, PDF
    # лид-магнитов) — по решению Николь 2026-07-21 они уходят с продажного
    # номера 84 (971589217784), чтобы диалог клиента сразу жил в переписке
    # менеджера. Пусто → фолбэк на WAZZUP_CHANNEL_ID (сервисный канал кодов).
    # ⚠️ На 2026-07-21 WhatsApp-канал 84 в Wazzup blocked/qridle — перед
    # заполнением переменной переподключить канал (QR) в кабинете Wazzup.
    WAZZUP_CLIENT_CHANNEL_ID: str = os.getenv("WAZZUP_CLIENT_CHANNEL_ID", "")
    # Резервные каналы через запятую (2026-07-27, решение Николь: «если 84 не
    # работает — коды и чек-листы уходят с 14»). WhatsApp-канал живёт по QR и
    # регулярно отваливается в qridle; без резерва это тихо ломает вход в кабинет
    # и доставку чек-листов. Порядок важен: пробуем слева направо после основного.
    WAZZUP_FALLBACK_CHANNEL_IDS: str = os.getenv("WAZZUP_FALLBACK_CHANNEL_IDS", "")
    # Предохранитель Telegram-дайджеста (Фаза 4). По умолчанию OFF: планировщик 5/20
    # работает в dry (превью в лог), реально НЕ шлёт. Включить только осознанно:
    # DIGEST_ENABLED=1 в Railway, когда агенты уже в боте и формат подтверждён.
    DIGEST_ENABLED: bool = os.getenv("DIGEST_ENABLED", "") in ("1", "true", "True")
    # ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ уведомлений партнёрам (Фаза K, план 2026-05-27).
    # default FALSE: пока агентов не пригласили в кабинет ([[feedback_no_agent_outreach_yet]]),
    # НИЧЕГО наружу не уходит — каждый триггер логируется в notification_attempts
    # со status='dry_run', в сеть 0 пакетов. Включать ТОЛЬКО осознанно в Railway
    # env (NOTIFICATIONS_LIVE=true) по команде Николь, НЕ дефолтом в коде.
    # Доп. слой для WhatsApp — WAZZUP_TEST_ONLY_NUMBER (шлёт только на тестовый).
    NOTIFICATIONS_LIVE: bool = os.getenv("NOTIFICATIONS_LIVE", "") in ("1", "true", "True")
    ADMIN_TG_ID: int = int(os.getenv("ADMIN_TG_ID", "6634813047"))
    # Трекинг использования портала (план 2026-06-03): partner_id, которые НЕ
    # попадают в статистику заходов (шум от тестов). Сама Николь (ADMIN_TG_ID)
    # исключается автоматически. CSV, напр. "141". Тест-партнёр 141 — временный
    # (identity 119 на нём), при возврате на partner 1 эту настройку можно убрать.
    USAGE_EXCLUDE_PARTNER_IDS: set[int] = {
        int(x) for x in os.getenv("USAGE_EXCLUDE_PARTNER_IDS", "141").split(",") if x.strip()
    }
    # Куда ЕЩЁ дублировать заявки, кроме личного Telegram владельца (2026-07-21).
    # Фолбэк на время, пока api не создаёт сделки в Kommo (диагностика 21.07:
    # заявка принята, ответ api — failed, сделки в CRM нет): менеджер получает
    # заявку в закрытую группу и заносит её в CRM руками. CSV chat_id; у группы
    # id отрицательный (напр. "-1001234567890"). Пусто = прежнее поведение.
    # ⚠️ В этих сообщениях ПД клиентов — только закрытые чаты своей команды.
    NOTIFY_TG_CHAT_IDS: list[str] = [
        x.strip() for x in os.getenv("NOTIFY_TG_CHAT_IDS", "").split(",") if x.strip()
    ]
    CONTACT_TG_USERNAME: str = os.getenv("CONTACT_TG_USERNAME", "nikol_hillton")
    CONTACT_WA_NUMBER: str = os.getenv("CONTACT_WA_NUMBER", "971589217784")
    # ─── Мониторинг ссылок (план 2026-07-23) ─────────────────────────────────
    # Алерты Николь о поломке ссылок идут в её TG (внутренний канал, как
    # _notify_admin_new_quiz), поэтому NOTIFICATIONS_LIVE их НЕ гейтит. Собственные
    # предохранители — default OFF: сперва dry-summary в лог, калибровка на реальном
    # трафике, потом включение в Railway env.
    # LINK_HEALTH_ALERTS — однозначные поломки (лендинг 5xx/404, битый конфиг целей).
    LINK_HEALTH_ALERTS: bool = os.getenv("LINK_HEALTH_ALERTS", "") in ("1", "true", "True")
    # LINK_HEALTH_STATS_ALERTS — статистические сигналы (перестало собирать переходы /
    # конверсия просела). Малая выборка → шумят; по умолчанию только видны в /admin.
    LINK_HEALTH_STATS_ALERTS: bool = os.getenv("LINK_HEALTH_STATS_ALERTS", "") in ("1", "true", "True")
    # Окна детекторов. STALE — за сколько часов «тишины» ссылка считается затухшей;
    # BASELINE_DAYS — окно базлайна; MIN_BASELINE_CLICKS — ниже этого базлайна выводов
    # не делаем (мало данных). KOMMO_FAIL_WINDOW_HOURS — окно распределения kommo_status.
    LINK_STALE_HOURS: int = int(os.getenv("LINK_STALE_HOURS", "72"))
    LINK_BASELINE_DAYS: int = int(os.getenv("LINK_BASELINE_DAYS", "30"))
    LINK_MIN_BASELINE_CLICKS: int = int(os.getenv("LINK_MIN_BASELINE_CLICKS", "20"))
    KOMMO_FAIL_WINDOW_HOURS: int = int(os.getenv("KOMMO_FAIL_WINDOW_HOURS", "24"))
    # Порт локальной self-пробы лендингов (http://127.0.0.1:$PORT, НЕ WEBAPP_URL:
    # тот в дефолте localhost:8000 и дал бы вечный ложный «лендинг лежит»). Railway
    # задаёт PORT в env.
    HEALTH_PROBE_PORT: str = os.getenv("PORT", "8000")
    # Баннер набора на Mastermind в разделе «Курсы». Меняется по месяцам — из конфига,
    # не хардкодом в шаблоне (правило репо №1). Пустой MASTERMIND_TITLE — баннер скрыт.
    # Сейчас СКРЫТ (решение Николь 2026-06-02): набор закрыт. Чтобы вернуть — задать
    # MASTERMIND_TITLE/_EN (env или дефолт) с новым текстом месяца.
    MASTERMIND_TITLE: str = os.getenv(
        "MASTERMIND_TITLE",
        "",
    )
    # Программа: 5 AI-сотрудников, по пунктам через «;» (в баннере — через запятую).
    # Пункты 4–5 — заглушки до уточнения.
    MASTERMIND_DETAILS: str = os.getenv(
        "MASTERMIND_DETAILS",
        "Анализ конкурентов; "
        "Тексты для рассылок, постов и рилсов; "
        "Рассылки из CRM и Telegram-бота; "
        "Уточняется; "
        "Уточняется",
    )
    MASTERMIND_FOOTER: str = os.getenv(
        "MASTERMIND_FOOTER",
        "Старт 26 мая. Осталось 3 места из 10.",
    )
    # Английские версии баннера — выбираются в шаблоне при lang == 'en' (как title_en у курсов).
    # Без них EN-витрина показывала русский текст баннера.
    MASTERMIND_TITLE_EN: str = os.getenv(
        "MASTERMIND_TITLE_EN",
        "",
    )
    MASTERMIND_DETAILS_EN: str = os.getenv(
        "MASTERMIND_DETAILS_EN",
        "Competitor analysis; "
        "Copy for broadcasts, posts and reels; "
        "Broadcasts from CRM and Telegram bot; "
        "TBA; "
        "TBA",
    )
    MASTERMIND_FOOTER_EN: str = os.getenv(
        "MASTERMIND_FOOTER_EN",
        "Starts May 26. 3 of 10 seats left.",
    )
    # Обучающие видео в разделе «Обучение» (решение Николь 2026-07-23): два ролика
    # YouTube «доступ по ссылке» над курсами. ID — из конфига, не хардкодом (правило №1).
    # Пустой ID — карточка видео скрыта.
    TRAINING_VIDEO_PROGRAM_ID: str = os.getenv("TRAINING_VIDEO_PROGRAM_ID", "O1qprCkhXBM")
    TRAINING_VIDEO_CABINET_ID: str = os.getenv("TRAINING_VIDEO_CABINET_ID", "__7kQAR0Yjo")


settings = Settings()
