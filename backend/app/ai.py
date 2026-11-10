"""ИИ-слой: разбор обращения, извлечение пунктов из совещаний, сводка, поиск похожих.

Движки с одинаковым результатом:
- claude: Claude через официальный SDK (ANTHROPIC_API_KEY), структурированный вывод;
- openai: OpenAI-совместимый Chat Completions (OPENAI_API_KEY), строгая JSON-схема;
- rules:  правила и словари - работают без сети и ключей и включаются автоматически,
          если ключа нет, модель недоступна или отказалась отвечать.
Ответ модели всегда проверяется по той же pydantic-схеме, поэтому остальной код не знает, какой движок отработал.
Поиск похожих обращений всегда локальный (лексическая близость), данные никуда не уходят.
"""
from __future__ import annotations

import copy
import logging
import math
import re
from collections import Counter
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import settings

log = logging.getLogger("ai")

TypeT = Literal["problem", "proposal", "praise"]
PriorityT = Literal["low", "medium", "high"]


class Triage(BaseModel):
    type: TypeT
    category: str = Field(description="Название категории строго из переданного списка")
    priority: PriorityT
    title: str = Field(description="Заголовок до 80 символов, по сути, без слов 'проблема' и 'предложение'")
    summary: str = Field(description="Суть в одном-двух предложениях для руководителя")
    question: str = Field(description="Один уточняющий вопрос автору, если без ответа нельзя начать работу; иначе пустая строка")


class MeetingPoint(BaseModel):
    kind: Literal["problem", "proposal", "praise", "decision", "task"]
    title: str
    text: str = Field(description="Самодостаточная формулировка: понятна тому, кто не был на совещании")
    quote: str = Field(description="Дословный фрагмент расшифровки, на котором основан пункт")
    speaker: str = Field(description="Кто сказал, если это понятно из расшифровки; иначе пустая строка")
    category: str = Field(description="Название категории строго из переданного списка")
    priority: PriorityT


class MeetingExtract(BaseModel):
    summary: str = Field(description="О чем было совещание, 2-4 предложения")
    points: list[MeetingPoint]


_client = None


def _claude():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=settings.anthropic_key, timeout=120.0)
    return _client


def _parse(system: str, user: str, schema: type[BaseModel], effort: str = "low"):
    """Один вызов модели со структурированным ответом. None - если ответа нет (переходим на правила)."""
    if settings.llm_provider == "openai":
        return _parse_openai(system, user, schema, effort)
    if settings.llm_provider != "claude":
        return None
    import anthropic
    try:
        resp = _claude().messages.parse(
            model=settings.llm_model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
            output_format=schema,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            log.warning("Claude не вернул результат (stop_reason=%s), работаем по правилам", resp.stop_reason)
            return None
        return resp.parsed_output
    except anthropic.AuthenticationError:
        log.error("Claude: неверный ANTHROPIC_API_KEY, работаем по правилам")
    except anthropic.RateLimitError:
        log.warning("Claude: превышен лимит запросов, работаем по правилам")
    except anthropic.APIStatusError as e:
        log.warning("Claude: ошибка API %s (%s), работаем по правилам", e.status_code, e.message)
    except anthropic.APIConnectionError:
        log.warning("Claude: нет связи, работаем по правилам")
    return None


def _strict(node):
    """Схема pydantic -> строгая схема OpenAI: у каждого объекта все поля обязательны и лишних нет.

    Служебные title убираем только у схем, а не внутри properties: у нас есть поле, которое так и называется - title.
    """
    if isinstance(node, list):
        return [_strict(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in ("title", "default", "description") and not isinstance(v, dict):
            if k == "description":
                out[k] = v  # описания полей - подсказки модели, их оставляем
            continue
        if k in ("properties", "$defs"):
            out[k] = {name: _strict(sub) for name, sub in v.items()}
        else:
            out[k] = _strict(v)
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


_effort_ok = True  # reasoning_effort понимают не все модели и шлюзы: если не принят - больше не передаем


def _parse_openai(system: str, user: str, schema: type[BaseModel], effort: str):
    global _effort_ok
    fmt = {"type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True,
                                                   "schema": _strict(copy.deepcopy(schema.model_json_schema()))}}
    body = {"model": settings.openai_model, "max_completion_tokens": 16000, "response_format": fmt,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if _effort_ok:
        body["reasoning_effort"] = effort
    headers = {"Authorization": f"Bearer {settings.openai_key}"}
    url = f"{settings.openai_base_url}/chat/completions"
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=180.0)
        if r.status_code == 400 and "reasoning_effort" in r.text and "reasoning_effort" in body:
            log.warning("OpenAI: reasoning_effort не принят, продолжаем без него")
            _effort_ok = False
            body.pop("reasoning_effort")
            r = httpx.post(url, json=body, headers=headers, timeout=180.0)
        if r.status_code in (401, 403):
            log.error("OpenAI: ключ не принят (%s), работаем по правилам", r.status_code)
            return None
        if r.status_code == 429:
            log.warning("OpenAI: превышен лимит или закончились средства, работаем по правилам")
            return None
        if r.status_code >= 400:
            log.warning("OpenAI: ошибка %s: %s - работаем по правилам", r.status_code, r.text[:300])
            return None
        choice = r.json()["choices"][0]
        msg = choice.get("message") or {}
        if msg.get("refusal") or choice.get("finish_reason") in ("content_filter", "length"):
            log.warning("OpenAI не вернул результат (%s), работаем по правилам", msg.get("refusal") or choice.get("finish_reason"))
            return None
        return schema.model_validate_json(msg.get("content") or "")
    except (httpx.HTTPError, ValidationError, KeyError, ValueError) as e:
        log.warning("OpenAI: %s, работаем по правилам", e)
        return None


def engine_name() -> str:
    return f"{settings.llm_provider} ({settings.llm_label})" if settings.llm_enabled else "rules"


def _engine() -> str:
    """Что записать как движок, разобравший данные: модель или rules."""
    return settings.llm_label


# ---------- разбор обращения ----------

TRIAGE_SYSTEM = """Ты помогаешь регистрировать обращения сотрудников ГК «Модус Трейд» - компании, которая \
поставляет и внедряет системы безопасности (видеонаблюдение, пожарная безопасность, контроль доступа): \
продажи, закупки, проектирование, пусконаладка, сервис, IT, бухгалтерия.

Сотрудник написал обращение в свободной форме - в веб-форме, в Telegram или письмом. Твоя задача - \
подготовить карточку, чтобы обращение сразу попало к нужному руководителю и не потерялось.

Тип: problem - что-то мешает работе; proposal - идея, как сделать лучше; praise - благодарность или \
положительный отзыв. Если сотрудник описал проблему и тут же предложил решение, это proposal.
Приоритет: high - срывает сроки, деньги, клиента или безопасность, либо блокирует работу нескольких людей; \
low - пожелание без срочности; иначе medium.
Категорию выбирай только из списка. Не придумывай деталей, которых нет в тексте. Уточняющий вопрос задавай, \
только если без него ответственный не поймет, о чем речь (например, не названа система или документ)."""


def triage(text: str, categories: list[tuple[str, str]]) -> tuple[Triage, str]:
    """Вернуть карточку и название движка. categories: [(название, описание)]."""
    names = [c[0] for c in categories]
    listing = "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in categories)
    out = _parse(TRIAGE_SYSTEM, f"Категории:\n{listing}\n\nОбращение:\n{text}", Triage)
    if out and out.category in names:
        out.title = out.title[:200]
        return out, _engine()
    return _triage_rules(text, categories), "rules"


_PRAISE = ("спасибо", "благодар", "хочу отметить", "отлично", "понравил", "здорово", "молодц")
_PROPOSAL = ("предлага", "было бы", "можно ли", "хотелось бы", "давайте", "стоит ", "идея", "нужна оценка", "полезн")
_HIGH = ("срочн", "срыв", "сорва", "простой", "клиент", "заказчик", "штраф", "авари", "блокиру", "не работает",
         "много времени", "повторн", "теря")
_LOW = ("было бы", "хотелось бы", "зона", "отдых", "перерыв", "уютн", "полезно")


def _triage_rules(text: str, categories: list[tuple[str, str]]) -> Triage:
    low = text.lower()
    if any(w in low for w in _PRAISE) and not any(w in low for w in ("но ", "однако", "не хватает", "проблем")):
        typ = "praise"
    elif any(w in low for w in _PROPOSAL):
        typ = "proposal"
    else:
        typ = "problem"
    if typ == "praise":
        prio = "low"
    elif any(w in low for w in _HIGH):
        prio = "high"
    elif any(w in low for w in _LOW):
        prio = "low"
    else:
        prio = "medium"
    # категория: ближайшая по словам к "название + описание"
    docs = [f"{n} {d}" for n, d in categories]
    scores = similarity(text, docs) if docs else []
    best = max(range(len(docs)), key=lambda i: scores[i]) if docs else None
    cat = categories[best][0] if best is not None else ""
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0]
    title = first[:80].rstrip(" .,;:") + ("..." if len(first) > 80 else "")
    return Triage(type=typ, category=cat, priority=prio, title=title or "Обращение", summary=text.strip()[:240], question="")


# ---------- совещания ----------

MEETING_SYSTEM = """Ты разбираешь расшифровку рабочего совещания в ГК «Модус Трейд» (системы безопасности: продажи, \
закупки, проектирование, пусконаладка, сервис, IT, бухгалтерия).

Собственник компании описал боль так: дельные замечания звучат на совещаниях и в разговорах, человек считает, \
что вопрос уже решается, а о нем забывают. Твоя задача - чтобы ни одно такое замечание не потерялось.

Выпиши из расшифровки отдельными пунктами:
- problem: кто-то сказал, что что-то мешает работе, даже вскользь;
- proposal: прозвучала идея улучшения;
- praise: благодарность или положительная оценка;
- decision: о чем договорились;
- task: кому-то поручили конкретное действие.

Каждый пункт формулируй так, чтобы он был понятен человеку, который не был на совещании. В quote приведи \
дословный фрагмент расшифровки - по нему человек проверит тебя перед тем, как пункт станет обращением. \
Расшифровка сделана автоматически, в ней бывают ошибки распознавания: исправляй очевидные по смыслу, но не \
придумывай того, чего не говорили. Приветствия, общие слова и организационные реплики пропускай. \
Одну мысль не дроби на несколько пунктов и не повторяй: если договоренность состоит в том, что кто-то что-то \
сделает к сроку, это один пункт task (кто, что, к какому сроку), а не отдельные decision и task. \
Категорию выбирай только из списка.

Приоритет: high - только если из слов следует, что срываются сроки, деньги, клиент или безопасность, либо \
работа нескольких людей встала; low - пожелание без срочности и благодарности; все остальное - medium. \
На обычной планерке большинство пунктов - medium."""


def extract_meeting(transcript: str, categories: list[tuple[str, str]]) -> tuple[MeetingExtract, str]:
    names = [c[0] for c in categories]
    listing = "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in categories)
    out = _parse(MEETING_SYSTEM, f"Категории:\n{listing}\n\nРасшифровка:\n{transcript}", MeetingExtract, effort="medium")
    if out:
        for p in out.points:
            if p.category not in names:
                p.category = ""
        return out, _engine()
    return _extract_rules(transcript, categories), "rules"


_M_PROBLEM = ("проблем", "не хватает", "сталкива", "сложно", "трудно", "забыл", "теря", "хаотич", "не понятно",
              "непонятно", "неясно", "задерж", "мешает", "не находят", "приходится", "ошибк", "нет ни ", "не понимаю",
              "не вижу", "не могу", "тратят", "тратим", "чуть не", "очень часто", "воз и ныне", "каждый раз")
_M_PRAISE = ("спасибо", "благодар", "хочу отметить", "молодц")
_NOT_SPEAKER = {"решили", "решение", "итог", "итого", "вопрос", "предложение", "проблема", "задача", "поручение",
                "договорились", "повестка", "тема"}
_M_PROPOSAL = ("предлага", "хотел бы", "хотелось бы", "давайте", "было бы", "нужно сделать", "нужен ", "нужна ",
               "необходимо", "стоит ")
_M_DECISION = ("решили", "договорились", "принято решение", "утвердили", "согласовали")
_M_TASK = ("поручаю", "ответственный -", "к понедельнику", "к пятнице", "сделать до", "срок до", "возьми на себя")


def _extract_rules(transcript: str, categories: list[tuple[str, str]]) -> MeetingExtract:
    """Запасной разбор без модели: ищем фразы-маркеры. Грубее модели, но совещание не теряется."""
    lines = [re.sub(r"^\[\d+:\d+(?::\d+)?\]\s*", "", ln).strip() for ln in transcript.splitlines() if ln.strip()]
    # реплика = строка «Имя: текст»; сплошной текст (автоматическая расшифровка) режем на предложения
    units: list[tuple[str, str]] = []
    for ln in lines:
        m = re.match(r"^([А-ЯЁA-Z][\w.\- ]{1,40}?):\s+(.+)$", ln)
        if m and m.group(1).lower() in _NOT_SPEAKER:  # «Решили: ...» - это не имя говорящего
            m = None
        speaker, body = (m.group(1), m.group(2)) if m else ("", ln)
        if m and len(body) <= 450:
            units.append((speaker, body))
        else:
            units += [(speaker, s.strip()) for s in re.split(r"(?<=[.!?])\s+", body)]
    if not any(sp for sp, _ in units):  # в расшифровке без имен склеиваем обрывки строк в предложения
        joined = " ".join(b for _, b in units)
        units = [("", s.strip()) for s in re.split(r"(?<=[.!?])\s+", joined)]
    points: list[MeetingPoint] = []
    seen: list[str] = []
    for speaker, s in units:
        if len(s) < 25:
            continue
        low = s.lower()
        kind = None
        for k, words in (("decision", _M_DECISION), ("task", _M_TASK), ("proposal", _M_PROPOSAL), ("problem", _M_PROBLEM),
                         ("praise", _M_PRAISE)):
            if any(w in low for w in words):
                kind = k
                break
        if not kind or (seen and max(similarity(s, seen)) > 0.6):
            continue
        seen.append(s)
        t = _triage_rules(s, categories)
        points.append(MeetingPoint(kind=kind, title=t.title, text=s, quote=s, speaker=speaker, category=t.category, priority=t.priority))
    return MeetingExtract(summary=f"Автоматический разбор по фразам-маркерам: найдено пунктов - {len(points[:25])}.", points=points[:25])


# ---------- сводка для руководителя ----------

class Theme(BaseModel):
    title: str = Field(description="Название темы своими словами, до 60 символов")
    numbers: list[str] = Field(description="Номера обращений этой темы, минимум два")
    note: str = Field(description="Одно предложение: что общего и почему это системный вопрос")


class DigestOut(BaseModel):
    text: str = Field(description="Сводка в формате Markdown: короткие абзацы и списки, без заголовка первого уровня")
    themes: list[Theme] = Field(description="Повторяющиеся темы: группы обращений об одной и той же первопричине")


DIGEST_SYSTEM = """Ты готовишь сводку для собственника ГК «Модус Трейд» по обращениям сотрудников. Он открывает ее, \
чтобы за две минуты понять, что в компании требует его внимания, и не вспоминать совещания по памяти.

Напиши по-русски, по делу, без вступлений и без канцелярита:
1. Что требует решения сейчас - просроченное, высокий приоритет, то, что давно без движения. Называй номера обращений.
2. Повторяющиеся темы - где несколько людей говорят об одном и том же; это сигнал о системной проблеме.
3. Что уже сделано - реализованные предложения, которые стоит показать команде.
Опирайся только на переданные данные, числа бери из них как есть: «просрочено на N дн.» - это просрочка, \
а «поступило дней назад» - возраст обращения, не путай их. Если в каком-то разделе нечего сказать, пропусти его. \
Пиши очень коротко: не больше семи пунктов на всю сводку, каждый пункт - одна строка до 12 слов, всего до 90 слов. \
Выбирай главное, а не перечисляй все подряд.

Отдельно верни список повторяющихся тем. Тема - это несколько обращений (от двух), у которых общая первопричина, \
даже если они в разных категориях и написаны разными словами: например, три человека из разных отделов по-разному \
говорят, что не видят, на каком этапе находится работа. Не объединяй обращения только из-за общей категории или \
похожих слов. Обращение, не похожее на остальные, ни в какую тему не включай."""


def digest(facts: str) -> tuple[DigestOut | None, str]:
    out = _parse(DIGEST_SYSTEM, facts, DigestOut, effort="medium")
    if not out:
        return None, "rules"
    out.text = re.sub(r"\s*[—–]\s*", " - ", out.text)  # единый стиль: обычный дефис вместо длинного тире
    return out, _engine()


# ---------- лексическая близость (локально) ----------

_STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня
еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто
этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при
наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою
этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между это нужен нужна нужно предлагаю хотелось""".split())


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))
    # грубый стемминг: длинные слова режем до основы - для русского этого хватает, чтобы сближать словоформы
    return [w[:6] if len(w) > 6 else w for w in words if len(w) > 2 and w not in _STOP]


def similarity(query: str, docs: list[str]) -> list[float]:
    """Косинусная близость TF-IDF между запросом и каждым документом."""
    if not docs:
        return []
    toks = [_tokens(d) for d in docs]
    q = _tokens(query)
    n = len(docs) + 1
    df = Counter(t for d in toks + [q] for t in set(d))

    def vec(ts: list[str]) -> dict[str, float]:
        tf = Counter(ts)
        return {t: (1 + math.log(c)) * (math.log((n + 1) / (df[t] + 0.5))) for t, c in tf.items()}

    def cos(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(v * b.get(t, 0.0) for t, v in a.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return dot / (na * nb) if na and nb else 0.0

    qv = vec(q)
    return [cos(qv, vec(t)) for t in toks]


def clusters(texts: list[str], threshold: float = 0.22) -> list[list[int]]:
    """Жадная группировка похожих текстов. Возвращает группы индексов размером от двух."""
    groups: list[list[int]] = []
    for i, t in enumerate(texts):
        best, best_s = None, threshold
        for g in groups:
            s = max(similarity(t, [texts[j] for j in g]))
            if s > best_s:
                best, best_s = g, s
        if best is not None:
            best.append(i)
        else:
            groups.append([i])
    return [g for g in groups if len(g) > 1]


def top_terms(texts: list[str], k: int = 3) -> list[str]:
    words = Counter()
    for t in texts:
        for w in set(re.findall(r"[а-яё]{5,}", t.lower())):
            if w[:6] not in _STOP and w not in _STOP:
                words[w] += 1
    return [w for w, c in words.most_common(k) if c > 1]
