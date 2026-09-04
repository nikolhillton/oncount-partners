"""Реестр лид-магнитов по всем чек-листам (2026-07-27).

Единый движок поверх quiz.html и _handle_quiz_submit — как /guide/corp-tax и
/guide/5-mistakes, но темы описаны данными, а не отдельными модулями: один
словарь TOPICS вместо десяти копий конфига. Роуты в main.py — универсальные
(/guide/{slug} + /guide/{slug}/submit), первые два лид-магнита оставлены на
своих выделенных роутах и в этот реестр НЕ входят.

Правило Николь (2026-07-27): чек-лист клиенту — ТОЛЬКО через квиз (телефон →
PDF-ссылка в WhatsApp, лид с ?ref= привязан к агенту). Прямые PDF не раздаём.

Вопросы квалификации у всех тем общие — из leadmagnet_config (стабильные id
company/field/priority: по ним пишутся answers и примечание лида в Kommo).
PDF — публичные Google Drive-ссылки (доступ «по ссылке» включён 2026-07-27).
Если PDF есть только на одном языке — шлём его же с пометкой в WA-тексте.

⚠️ Ссылки на эти квизы в партнёрский кабинет (/links) НЕ добавлены — решение
Николь «в кабинет пока не выкладывать». Выдача агентам — отдельной командой.
"""

from app.leadmagnet_config import (
    FINAL,
    FINAL_EN,
    INTRO,
    INTRO_EN,
    QUESTION_TITLES,
    QUESTIONS,
    QUESTIONS_EN,
    SOCIALS,
    SOCIALS_EN,
    VALID_OPTIONS,
)

__all__ = [
    "TOPICS", "VALID_OPTIONS", "QUESTION_TITLES",
    "page", "wa_text", "submit_rl_paths",
]

_DRIVE = "https://drive.google.com/file/d/{fid}/view"


def _cover(badge: str, title: str, lead: str, bullets: list[str],
           cta: str, note: str) -> dict:
    return {"image": None, "badge": badge, "title": title, "lead": lead,
            "bullets": bullets, "cta": cta, "note": note}


# Каждая тема: name_* — как чек-лист называется в WA-сообщении; value_* — одна
# фраза пользы там же; pdf_ru/pdf_en — Drive-id (None = на этом языке PDF нет).
TOPICS: dict[str, dict] = {
    "buh-mistakes": {
        "name_ru": "9 ошибок, за которые стоит уволить бухгалтера в ОАЭ",
        "name_en": "9 red flags in accounting in the UAE",
        "pdf_ru": "1DhgA-D-jE48mjxeVdoNUdPc992IMqnVg",
        "pdf_en": "18855EnNmAKpRlK6fRsIzlJBghuzHR7mD",
        "value_ru": ("Он поможет за 5 минут проверить своего бухгалтера: какие "
                     "ошибки ведут к штрафам FTA и какие вопросы стоит задать "
                     "уже сегодня."),
        "value_en": ("It helps you audit your accountant in 5 minutes: which "
                     "mistakes lead to FTA fines and what questions to ask "
                     "today."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "9 ошибок бухгалтера в ОАЭ, которые стоят вам штрафов",
            "Проверьте своего бухгалтера за 5 минут: 9 красных флагов, за "
            "которые FTA выписывает штрафы, а бизнес теряет деньги. Ответьте "
            "на 3 вопроса — пришлём PDF прямо в WhatsApp.",
            ["9 красных флагов в работе бухгалтера",
             "Какие ошибки приводят к штрафам FTA",
             "Вопросы, которые стоит задать бухгалтеру сегодня"],
            "Получить чек-лист",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "9 red flags in your UAE accounting that cost you fines",
            "Audit your accountant in 5 minutes: 9 red flags the FTA fines "
            "for. Answer 3 questions — we will send the PDF straight to your "
            "WhatsApp.",
            ["9 red flags in your accountant's work",
             "Which mistakes lead to FTA fines",
             "Questions worth asking your accountant today"],
            "Get the checklist",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "ai-sotrudnik": {
        "name_ru": "AI-сотрудник вместо промпта",
        "name_en": "An AI employee instead of a prompt",
        "pdf_ru": "1bJ3I1BXA13agyKMEKmso1hTjXBjvyLF0",
        "pdf_en": None,
        "value_ru": ("Внутри — пять промптов, которые считают ваши деньги, и разбор, "
                     "чем собранный AI-сотрудник отличается от чата: где он живёт, "
                     "какие уровни реальны и сколько он возвращает."),
        "value_en": ("Inside — five prompts that count your money and a breakdown of "
                     "how a built AI employee differs from a chat: where it lives, "
                     "which levels are realistic and what it brings back."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "AI-сотрудник вместо промпта",
            "Что можно отдать машине уже сегодня, что собирается за три дня и сколько "
            "денег это возвращает. Ответьте на 3 вопроса — пришлём PDF в WhatsApp.",
            ["Пять промптов, которые считают прибыль, налоги и кассовые разрывы",
             "Чем сотрудник отличается от чата и где он живёт",
             "Пять примеров с расчётом, сколько каждый возвращает"],
            "Получить чек-лист",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "An AI employee instead of a prompt",
            "What you can hand over to a machine today, what takes three days to build "
            "and how much money it brings back. Answer 3 questions — we will send the "
            "PDF to your WhatsApp.",
            ["Five prompts that count profit, tax and cash gaps",
             "How an employee differs from a chat and where it lives",
             "Five examples with the maths of what each one returns"],
            "Get the checklist",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "ai-tools": {
        "name_ru": "AI-инструменты для бизнеса",
        "name_en": "Essential AI tools for business growth",
        "pdf_ru": "1mnwEq2-f4DEs6xUU-JB4WiAqnk7IBON3",
        "pdf_en": "1KYXQEMA0iZcdGbUHri7c77fBltn8LQ6Y",
        "value_ru": ("Внутри — проверенные AI-инструменты для рутины бизнеса: "
                     "что автоматизировать в первую очередь и сколько это "
                     "стоит."),
        "value_en": ("Inside — proven AI tools for business routine: what to "
                     "automate first and what it costs."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "AI-инструменты, которые экономят бизнесу часы каждую неделю",
            "Подборка проверенных AI-инструментов для предпринимателя в ОАЭ: "
            "финансы, документы, клиенты. Ответьте на 3 вопроса — пришлём PDF "
            "в WhatsApp.",
            ["Проверенные AI-инструменты по задачам бизнеса",
             "Что автоматизировать в первую очередь",
             "Сколько это стоит и с чего начать"],
            "Получить чек-лист",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "AI tools that save your business hours every week",
            "A curated list of proven AI tools for UAE entrepreneurs: "
            "finance, documents, clients. Answer 3 questions — we will send "
            "the PDF to your WhatsApp.",
            ["Proven AI tools mapped to business tasks",
             "What to automate first",
             "What it costs and where to start"],
            "Get the checklist",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "corp-bank": {
        "name_ru": "Корпоративный счёт в ОАЭ",
        "name_en": "Corporate bank account in the UAE",
        "pdf_ru": "16LtWe2I3kSf1sGnC2CMAuDhoH2mlkl6Q",
        "pdf_en": "1TV6DxzLiHwcWP65i3TPXIzNq5Yegbbkj",
        "value_ru": ("Он поможет открыть корпоративный счёт с первого раза: "
                     "какие документы запросит банк и за что чаще всего "
                     "отказывают."),
        "value_en": ("It helps you open a corporate account on the first try: "
                     "what documents the bank asks for and the most common "
                     "rejection reasons."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "Корпоративный счёт в ОАЭ: как открыть с первого раза",
            "Банки ОАЭ отказывают без объяснения причин. Разбор: документы, "
            "красные флаги комплаенса и как поднять шансы. Ответьте на 3 "
            "вопроса — пришлём PDF в WhatsApp.",
            ["Какие документы запросит банк",
             "За что банки чаще всего отказывают",
             "Как повысить шансы открыть счёт быстро"],
            "Получить чек-лист",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "Corporate bank account in the UAE: open it on the first try",
            "UAE banks decline without explanations. A breakdown: documents, "
            "compliance red flags and how to raise your chances. Answer 3 "
            "questions — we will send the PDF to your WhatsApp.",
            ["Documents the bank will request",
             "The most common rejection reasons",
             "How to raise your chances of a fast opening"],
            "Get the checklist",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "tax-optimize": {
        "name_ru": "Как легально снизить налоговую нагрузку в ОАЭ",
        "name_en": "How to legally reduce your tax burden in the UAE",
        "pdf_ru": "144WTIYfWlTl_b51r2xeaQq2nV5sZQiGB",
        "pdf_en": None,
        "value_ru": ("Внутри — легальные инструменты снижения налогов и "
                     "ошибки, которые приводят к доначислениям."),
        "value_en": ("Inside — legal tools to reduce taxes and the mistakes "
                     "that lead to reassessments."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "Как легально снизить налоговую нагрузку в ОАЭ",
            "Разбор легальных инструментов: структура, qualifying income, "
            "вычеты — и ошибки, за которые FTA доначисляет. Ответьте на 3 "
            "вопроса — пришлём PDF в WhatsApp.",
            ["Легальные инструменты снижения налогов",
             "Ошибки, которые приводят к доначислениям",
             "Что проверить до конца отчётного периода"],
            "Получить чек-лист",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "How to legally reduce your tax burden in the UAE",
            "Legal tools: structure, qualifying income, deductions — and the "
            "mistakes the FTA reassesses for. Answer 3 questions — we will "
            "send the PDF to your WhatsApp (the guide is in Russian).",
            ["Legal tools to reduce taxes",
             "Mistakes that lead to reassessments",
             "What to check before the reporting period ends"],
            "Get the checklist",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "company-setup": {
        "name_ru": "Пошаговый план открытия компании в ОАЭ",
        "name_en": "Company setup in the UAE, step by step",
        "pdf_ru": "157YCF_jud4jeP1msVLniZLE2M84t2CSa",
        "pdf_en": "1Ws4ZnWRRumXipGagEnCy5akRxk8y23LR",
        "value_ru": ("Он проведёт по всем шагам открытия: от выбора фризоны "
                     "до счёта — со сроками и стоимостью каждого шага."),
        "value_en": ("It walks you through every setup step: from choosing a "
                     "Freezone to the bank account — with timelines and "
                     "costs."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "Пошаговый план открытия компании в ОАЭ",
            "Все шаги: фризона или mainland, лицензия, виза, счёт — со "
            "сроками, стоимостью и типичными ошибками новичков. Ответьте на "
            "3 вопроса — пришлём PDF в WhatsApp.",
            ["Все шаги от выбора фризоны до счёта",
             "Сроки и стоимость каждого шага",
             "На чём теряют деньги при открытии"],
            "Получить план",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "Company setup in the UAE: a step-by-step plan",
            "Every step: Freezone or mainland, licence, visa, bank account — "
            "with timelines, costs and typical first-timer mistakes. Answer "
            "3 questions — we will send the PDF to your WhatsApp.",
            ["Every step from Freezone choice to bank account",
             "Timelines and cost of each step",
             "Where first-timers lose money"],
            "Get the plan",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "ai-prompts": {
        "name_ru": "12 AI-промтов для финансов",
        "name_en": "12 AI prompts for finance",
        "pdf_ru": "1HiYR7hSpVp-Pl5UoRkniJkkbTO8ObY-y",
        "pdf_en": None,
        "value_ru": ("Внутри — 12 готовых промтов, которые снимают финансовую "
                     "рутину: вставляете и пользуетесь."),
        "value_en": ("Inside — 12 ready-made prompts that remove finance "
                     "routine: paste and use."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "12 готовых AI-промтов для финансов вашего бизнеса",
            "Готовые промты для ChatGPT/Claude: отчёты, план платежей, "
            "разбор расходов. Вставляете — и рутина уходит AI. Ответьте на 3 "
            "вопроса — пришлём PDF в WhatsApp.",
            ["12 готовых промтов для финансов и учёта",
             "Как поручить AI финансовую рутину",
             "Примеры «до/после» на реальных задачах"],
            "Получить промты",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "12 ready-made AI prompts for your business finance",
            "Ready prompts for ChatGPT/Claude: reports, payment plans, "
            "expense breakdowns. Paste them — and AI takes the routine. "
            "Answer 3 questions — we will send the PDF to your WhatsApp "
            "(the guide is in Russian).",
            ["12 ready prompts for finance and accounting",
             "How to hand finance routine to AI",
             "Before/after examples on real tasks"],
            "Get the prompts",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "profit-leaks": {
        "name_ru": "Расхитители прибыли",
        "name_en": "7 ways to save profit in your business",
        "pdf_ru": "1NgKwiLF7kIr7Um6O41WAUhWlSiBgC2Wj",
        "pdf_en": "1SN4IsSTfZSYVmR_pRjA4XT9HmvrrAPSI",
        "value_ru": ("Он показывает, где прибыль утекает незаметно, и 7 точек "
                     "экономии без потери качества."),
        "value_en": ("It shows where profit leaks unnoticed and 7 saving "
                     "points that do not hurt quality."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ЧЕК-ЛИСТ",
            "Расхитители прибыли: где ваш бизнес теряет деньги незаметно",
            "Аренда, подписки, налоги, персонал — где утечки и как их "
            "закрыть без потери качества. Ответьте на 3 вопроса — пришлём "
            "PDF в WhatsApp.",
            ["Где прибыль утекает незаметно",
             "7 точек экономии без потери качества",
             "Как найти свои утечки за один вечер"],
            "Получить чек-лист",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CHECKLIST",
            "Profit leaks: where your business loses money unnoticed",
            "Rent, subscriptions, taxes, staff — where the leaks are and how "
            "to close them without hurting quality. Answer 3 questions — we "
            "will send the PDF to your WhatsApp.",
            ["Where profit leaks unnoticed",
             "7 saving points that do not hurt quality",
             "How to find your leaks in one evening"],
            "Get the checklist",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "choose-accountant": {
        "name_ru": "Как выбрать бухгалтера в ОАЭ",
        "name_en": "How to choose an accountant in the UAE",
        "pdf_ru": None,
        "pdf_en": "1WG10AqBw7DbELZ5e_iK1WOKDqSRLfB06",
        "value_ru": ("Он поможет выбрать бухгалтера, который не подставит под "
                     "штрафы: критерии, вопросы на собеседовании, красные "
                     "флаги."),
        "value_en": ("It helps you choose an accountant who will not expose "
                     "you to fines: criteria, interview questions, red "
                     "flags."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ ГАЙД",
            "Как выбрать бухгалтера в ОАЭ и не пожалеть",
            "Критерии выбора, вопросы на собеседовании и красные флаги — "
            "чтобы не платить штрафы за чужие ошибки. Ответьте на 3 вопроса "
            "— пришлём PDF в WhatsApp (гайд на английском).",
            ["Критерии выбора бухгалтера в ОАЭ",
             "Вопросы, которые стоит задать до договора",
             "Красные флаги, при которых лучше уйти"],
            "Получить гайд",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE GUIDE",
            "How to choose an accountant in the UAE — and not regret it",
            "Selection criteria, interview questions and red flags — so you "
            "never pay fines for someone else's mistakes. Answer 3 questions "
            "— we will send the PDF to your WhatsApp.",
            ["Criteria for choosing a UAE accountant",
             "Questions to ask before signing",
             "Red flags that mean walk away"],
            "Get the guide",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "case-113500": {
        "name_ru": "Кейс: €113 500 экономии за год",
        "name_en": "Case study: €113,500 saved per year",
        "pdf_ru": None,
        "pdf_en": "1hdIqTNyqDR1oPpsbyb3y8k6h7L7lQW1h",
        "value_ru": ("Разбор реального кейса: за счёт чего компания в ОАЭ "
                     "сэкономила €113 500 за год — по шагам."),
        "value_en": ("A real case breakdown: how a UAE company saved €113,500 "
                     "in a year — step by step."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ КЕЙС",
            "Кейс: как компания в ОАЭ сэкономила €113 500 за год",
            "Реальный разбор: структура, налоги, расходы — какие решения "
            "дали экономию и что из этого применимо вам. Ответьте на 3 "
            "вопроса — пришлём PDF в WhatsApp (кейс на английском).",
            ["Какие решения дали €113 500 экономии",
             "Структура и налоги: что поменяли",
             "Что из кейса применимо вашему бизнесу"],
            "Получить кейс",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE CASE STUDY",
            "Case study: how a UAE company saved €113,500 in a year",
            "A real breakdown: structure, taxes, expenses — which decisions "
            "produced the savings and what applies to you. Answer 3 "
            "questions — we will send the PDF to your WhatsApp.",
            ["The decisions behind €113,500 of savings",
             "Structure and taxes: what was changed",
             "What from the case applies to your business"],
            "Get the case study",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
    "ai-stack": {
        "name_ru": "5 фрилансеров за $7000 или AI-стек за $300",
        "name_en": "5 freelancers for $7,000/m vs an AI stack for $300/m",
        "pdf_ru": None,
        "pdf_en": "1XDD-jWLFFUwtqqcSMI5bQdE2zwIw5YyG",
        "value_ru": ("Разбор: какие задачи фрилансеров сегодня закрывает "
                     "AI-стек за $300/мес и как его собрать."),
        "value_en": ("A breakdown: which freelancer tasks an AI stack covers "
                     "for $300/m today and how to build it."),
        "cover_ru": _cover(
            "БЕСПЛАТНЫЙ РАЗБОР",
            "5 фрилансеров за $7000/мес — или AI-стек за $300?",
            "Контент, дизайн, аналитика, поддержка: что уже можно отдать AI "
            "и сколько это стоит. Ответьте на 3 вопроса — пришлём PDF в "
            "WhatsApp (разбор на английском).",
            ["Какие задачи фрилансеров закрывает AI",
             "Стек инструментов и его цена по ролям",
             "С чего начать переход без потери качества"],
            "Получить разбор",
            "Бесплатно. PDF придёт в WhatsApp сразу после ответов."),
        "cover_en": _cover(
            "FREE BREAKDOWN",
            "5 freelancers for $7,000/m — or an AI stack for $300?",
            "Content, design, analytics, support: what you can hand to AI "
            "today and what it costs. Answer 3 questions — we will send the "
            "PDF to your WhatsApp.",
            ["Which freelancer tasks AI covers",
             "The tool stack and its cost per role",
             "Where to start without losing quality"],
            "Get the breakdown",
            "Free. The PDF arrives in WhatsApp right after your answers."),
    },
}

# RU-каркас WA-сообщения (как у corp-tax: {consult} подставляет _handle_quiz_submit).
_WA_RU = (
    "Здравствуйте! Это ONCOUNT 👋\n\n"
    "Вы запрашивали «{name}» — высылаю:\n{link}\n\n"
    "{value}\n\n"
    "И вы всегда можете записаться на бесплатную консультацию с нашим "
    "бухгалтером — разберём вашу ситуацию:\n{consult_ph}\n\n"
    "Или просто ответьте на это сообщение 🙂"
)
_WA_EN = (
    "Hello! This is ONCOUNT 👋\n\n"
    "You requested \"{name}\" — here it is{lang_note}:\n{link}\n\n"
    "{value}\n\n"
    "And you can always book a free consultation with our accountant — we'll "
    "go through your situation:\n{consult_ph}\n\n"
    "Or just reply to this message 🙂"
)

_THANKS_RU = {
    "title": "Готово! Материал уже в пути",
    "subtitle": ("Мы отправили PDF в WhatsApp на указанный номер. Не пришло "
                 "за пару минут — проверьте номер или напишите нам."),
}
_THANKS_EN = {
    "title": "Done! Your guide is on its way",
    "subtitle": ("We have sent the PDF to your WhatsApp number. If it does "
                 "not arrive within a couple of minutes, check the number or "
                 "message us."),
}


def _link(topic: dict, lang: str) -> str:
    """Drive-ссылка для языка; если на языке PDF нет — другой язык."""
    fid = topic["pdf_en"] if lang == "en" else topic["pdf_ru"]
    fid = fid or topic["pdf_ru"] or topic["pdf_en"]
    return _DRIVE.format(fid=fid)


def wa_text(slug: str, lang: str) -> str:
    """Готовый WA-текст (с подставленной PDF-ссылкой; {consult} оставляем
    плейсхолдером — его подставляет _handle_quiz_submit со ссылкой c ref)."""
    t = TOPICS[slug]
    if lang == "en":
        note = "" if t["pdf_en"] else " (the guide is in Russian)"
        return _WA_EN.format(name=t["name_en"], link=_link(t, "en"),
                             value=t["value_en"], lang_note=note,
                             consult_ph="{consult}")
    prefix = "" if t["pdf_ru"] else " (гайд на английском)"
    return _WA_RU.format(name=t["name_ru"] + prefix, link=_link(t, "ru"),
                         value=t["value_ru"], consult_ph="{consult}")


def page(slug: str, lang: str) -> dict:
    """Контекст quiz.html для темы и языка (как leadmagnet_config.page)."""
    t = TOPICS[slug]
    en = lang == "en"
    return {
        "cover": t["cover_en"] if en else t["cover_ru"],
        "intro": INTRO_EN if en else INTRO,
        "questions": QUESTIONS_EN if en else QUESTIONS,
        "final": FINAL_EN if en else FINAL,
        "thanks": _THANKS_EN if en else _THANKS_RU,
        "socials": SOCIALS_EN if en else SOCIALS,
    }


def page_title(slug: str, lang: str) -> str:
    t = TOPICS[slug]
    return "ONCOUNT — " + (t["name_en"] if lang == "en" else t["name_ru"])


def kommo(slug: str) -> dict:
    """Маркеры лида в Kommo — единообразно из имени темы."""
    t = TOPICS[slug]
    return {
        "prefix": f"Лид-магнит: {t['name_ru']}",
        "tag": f"guide-{slug}",
        "note": (f"Заявка с лид-магнита «{t['name_ru']}» "
                 "(чек-лист отправлен в WhatsApp)."),
        "header": f"📥 Новая заявка с лид-магнита «{t['name_ru']}»",
        "event_slug": f"guide-{slug}",
    }


def submit_rl_paths() -> tuple[str, ...]:
    """Submit-пути всех тем — для _RL_PATHS (анти-перебор чужих номеров)."""
    return tuple(f"/guide/{slug}/submit" for slug in TOPICS)
