"""Оргструктура из внешних систем: 1С:ЗУП (OData), Битрикс24 (REST) и файл выгрузки (CSV/XLSX).

Каждый источник отдает одинаковый «снимок» - подразделения и сотрудники, - а apply() сверяет его с базой:
заводит новых, обновляет должности и подразделения, отмечает отсутствующих и отключает уволенных.
Синхронизация только добавляет и обновляет данные, роли не понижает и ничего не удаляет.

Сопоставление: сначала по id во внешней системе (User.ext_ids), потом по почте; подразделения - по id, потом по
названию. Если адрес системы не задан, используется встроенный эмулятор (mocks.py) через подмену транспорта httpx -
запросы, разбор ответов и пагинация остаются теми же, что для настоящей системы.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import secrets
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl
from xml.etree import ElementTree

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import mocks
from .config import settings
from .models import Department, OrgSync, User, now
from .security import hash_password

log = logging.getLogger("orgsync")

SOURCES = {"1c": "1С:ЗУП", "b24": "Битрикс24", "file": "Файл выгрузки"}
TIMEOUT = 30


class SyncError(Exception):
    """Ошибка, которую можно показать администратору как есть."""


@dataclass
class Dept:
    ext: str
    name: str
    head: str | None = None  # ext сотрудника-руководителя


@dataclass
class Person:
    ext: str
    name: str
    email: str = ""
    position: str = ""
    dept: str | None = None  # ext подразделения
    active: bool = True
    absent: bool | None = None  # None - источник об отсутствиях не знает, поле не трогаем


@dataclass
class Snapshot:
    source: str
    depts: list[Dept] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    emulator: bool = False


def _today() -> date:
    return (now() + timedelta(hours=settings.tz_hours)).date()


def _http_error(system: str, e: Exception) -> SyncError:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hint = {401: "неверный логин или пароль", 403: "у пользователя нет прав", 404: "неверный адрес"}.get(code, "")
        return SyncError(f"{system}: ответ {code}{' - ' + hint if hint else ''}")
    if isinstance(e, httpx.TimeoutException):
        return SyncError(f"{system}: не ответил за {TIMEOUT} с")
    if isinstance(e, httpx.HTTPError):
        return SyncError(f"{system}: нет связи ({e.__class__.__name__})")
    return SyncError(f"{system}: непонятный ответ ({e})")


# ---------- 1С:ЗУП ----------

# Имена объектов и реквизитов типовой ЗУП 3. В конкретной базе их стоит сверить по $metadata - все в одном месте.
ONEC = {
    "deps": "Catalog_ПодразделенияОрганизаций",
    "positions": "Catalog_Должности",
    "persons": "Catalog_ФизическиеЛица",
    "employees": "Catalog_Сотрудники",
    "history": "InformationRegister_КадроваяИсторияСотрудников",
    "heads": "InformationRegister_РуководителиПодразделений",
    # документ отсутствия: (объект, реквизит начала, реквизит окончания, как назвать)
    "absences": [("Document_Отпуск", "ДатаНачалаОсновногоОтпуска", "ДатаОкончанияОсновногоОтпуска", "отпуск"),
                 ("Document_Командировка", "ДатаНачала", "ДатаОкончания", "командировка")],
}
EMPTY_REF = "00000000-0000-0000-0000-000000000000"


def _onec_client() -> tuple[httpx.Client, str, bool]:
    if settings.onec_odata_url:
        auth = (settings.onec_user, settings.onec_password) if settings.onec_user else None
        return httpx.Client(auth=auth, timeout=TIMEOUT), settings.onec_odata_url, False

    def handler(req: httpx.Request) -> httpx.Response:
        entity = req.url.path.rsplit("/", 1)[-1]
        try:
            return httpx.Response(200, json={"value": mocks.onec_entity(entity)})
        except KeyError:
            return httpx.Response(404, json={"odata.error": {"message": {"value": f"Не найден объект {entity}"}}})

    return httpx.Client(transport=httpx.MockTransport(handler)), "http://1c-emulator/zup/odata/standard.odata", True


def _d(s: str | None) -> date | None:
    if not s or s.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(s[:19]).date()
    except ValueError:
        return None


def fetch_1c() -> Snapshot:
    client, base, emulator = _onec_client()
    snap = Snapshot("1c", emulator=emulator)

    def get(entity: str, required: bool = True) -> list[dict]:
        try:
            r = client.get(f"{base}/{entity}", params={"$format": "json"})
            if r.status_code == 404 and not required:
                snap.warnings.append(f"В базе нет объекта {entity} - эти данные пропущены")
                return []
            r.raise_for_status()
            return r.json()["value"]
        except Exception as e:
            raise _http_error("1С", e) from e

    with client:
        deps = [d for d in get(ONEC["deps"]) if not d.get("DeletionMark")]
        positions = {p["Ref_Key"]: p["Description"] for p in get(ONEC["positions"])}
        emails = {}
        for p in get(ONEC["persons"]):
            for ci in p.get("КонтактнаяИнформация") or []:
                if ci.get("Тип") == "АдресЭлектроннойПочты" and ci.get("Представление"):
                    emails.setdefault(p["Ref_Key"], ci["Представление"].strip())
        employees = [e for e in get(ONEC["employees"]) if not e.get("DeletionMark")]
        last: dict[str, dict] = {}  # срез последних по кадровой истории
        for row in get(ONEC["history"]):
            k = row.get("Сотрудник_Key")
            if k and (k not in last or row.get("Period", "") >= last[k].get("Period", "")):
                last[k] = row
        heads = {r["Подразделение_Key"]: r["Руководитель_Key"] for r in get(ONEC["heads"], required=False)}
        today, absent = _today(), {}
        for entity, start, end, what in ONEC["absences"]:
            for doc in get(entity, required=False):
                s, e = _d(doc.get(start)), _d(doc.get(end))
                if doc.get("Posted") and not doc.get("DeletionMark") and s and e and s <= today <= e:
                    absent[doc.get("Сотрудник_Key")] = what

    snap.depts = [Dept(d["Ref_Key"], d["Description"].strip(), heads.get(d["Ref_Key"])) for d in deps]
    for e in employees:
        h = last.get(e["Ref_Key"], {})
        fired = bool(e.get("ВАрхиве")) or h.get("ВидСобытия") == "Увольнение"
        dep = h.get("Подразделение_Key")
        snap.people.append(Person(
            ext=e["Ref_Key"], name=e["Description"].strip(), email=emails.get(e.get("ФизическоеЛицо_Key"), ""),
            position=positions.get(h.get("Должность_Key"), ""), dept=dep if dep and dep != EMPTY_REF else None,
            active=not fired, absent=e["Ref_Key"] in absent))
    return snap


# ---------- Битрикс24 ----------

def _b24_client() -> tuple[httpx.Client, str, bool]:
    if settings.bitrix_webhook:
        return httpx.Client(timeout=TIMEOUT), settings.bitrix_webhook, False

    def handler(req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        params = dict(req.url.params)
        if req.content:
            params.update(parse_qsl(req.content.decode()))
        return httpx.Response(200, json=mocks.bitrix_call(method, params))

    return httpx.Client(transport=httpx.MockTransport(handler)), "http://b24-emulator/rest/1/demo", True


def fetch_b24() -> Snapshot:
    client, base, emulator = _b24_client()
    snap = Snapshot("b24", emulator=emulator)

    def get_all(method: str, params: dict) -> list[dict]:
        rows, start = [], 0
        try:
            while True:
                r = client.post(f"{base}/{method}.json", data={**params, "start": start})
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise SyncError(f"Битрикс24: {data.get('error_description') or data['error']}")
                rows += data.get("result") or []
                if "next" not in data or len(rows) > 20000:
                    return rows
                start = data["next"]
        except SyncError:
            raise
        except Exception as e:
            raise _http_error("Битрикс24", e) from e

    with client:
        deps = get_all("department.get", {})
        users = get_all("user.get", {"FILTER[USER_TYPE]": "employee"})
    # корень дерева без PARENT - сама компания (называется как портал), отделом у нас ее не заводим
    roots = {str(d["ID"]) for d in deps if not d.get("PARENT")} if len(deps) > 1 else set()
    snap.depts = [Dept(str(d["ID"]), str(d["NAME"]).strip(), str(d["UF_HEAD"]) if d.get("UF_HEAD") else None)
                  for d in deps if str(d["ID"]) not in roots]
    for u in users:
        name = " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x).strip() or u.get("EMAIL", "")
        dep = next((str(x) for x in u.get("UF_DEPARTMENT") or [] if str(x) not in roots), None)
        active = u.get("ACTIVE") not in (False, "N", "0", 0)
        snap.people.append(Person(ext=str(u["ID"]), name=name, email=(u.get("EMAIL") or "").strip(),
                                  position=(u.get("WORK_POSITION") or "").strip(),
                                  dept=dep, active=active))
    snap.warnings.append("Битрикс24 не передает отпуска и командировки - отметки «отсутствует» не меняются")
    return snap


# ---------- файл выгрузки ----------

COLUMNS = {
    "ext": ("табельный номер", "таб. номер", "таб номер", "табельный", "код", "id"),
    "name": ("фио", "сотрудник", "ф.и.о.", "имя"),
    "email": ("email", "e-mail", "почта", "электронная почта", "эл. почта"),
    "position": ("должность",),
    "dept": ("подразделение", "отдел"),
    "head": ("руководитель подразделения", "руководитель"),
    "absent": ("отсутствует", "в отпуске"),
    "absent_until": ("отсутствует до", "отпуск до"),
    "fired": ("уволен",),
}
TEMPLATE = ("Табельный номер;ФИО;Почта;Должность;Подразделение;Руководитель подразделения;Отсутствует до;Уволен\n"
            "0000000021;Камила Есенова;kamila@modus.demo;Бухгалтер;Бухгалтерия;;;\n"
            "0000000008;Тимур Жумабаев;nomen@modus.demo;Ответственный за номенклатуру;Закупки;;{until};\n")
YES = {"да", "1", "true", "yes", "x", "х", "+", "y"}


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """Первый лист .xlsx без сторонних библиотек: общие строки + ячейки."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
        rows = []
        for r in ElementTree.fromstring(z.read(sheet)).iter(f"{{{ns['m']}}}row"):
            row: list[str] = []
            for c in r.findall("m:c", ns):
                col = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(re.sub(r"\d", "", c.get("r", "A")))))
                v = c.find("m:v", ns)
                text = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t")) if c.get("t") == "inlineStr" else (v.text or "") if v is not None else ""
                if c.get("t") == "s" and text:
                    text = shared[int(text)]
                row += [""] * (col - 1 - len(row)) + [text]
            rows.append(row)
        return rows


def _excel_date(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d{5}(\.0+)?", s):  # дата в Excel хранится числом дней
        return date(1899, 12, 30) + timedelta(days=int(float(s)))
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


def parse_file(filename: str, data: bytes) -> Snapshot:
    snap = Snapshot("file")
    if filename.lower().endswith(".xlsx"):
        try:
            rows = _xlsx_rows(data)
        except Exception:
            raise SyncError("Не удалось прочитать .xlsx - сохраните лист как CSV")
    elif filename.lower().endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        first = text.split("\n", 1)[0]
        delim = max(";,\t", key=first.count)
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    else:
        raise SyncError("Нужен файл .csv или .xlsx")
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise SyncError("В файле нет строк с сотрудниками")
    header = [h.lower().replace("ё", "е") for h in rows[0]]
    col = {}
    for key, names in COLUMNS.items():
        for i, h in enumerate(header):
            if h in names and key not in col:
                col[key] = i
    missing = [n for n, k in (("ФИО", "name"), ("Почта", "email")) if k not in col]
    if missing:
        raise SyncError(f"Нет колонок: {', '.join(missing)}. Скачайте шаблон и сверьте заголовки")
    cell = lambda r, k: r[col[k]] if k in col and col[k] < len(r) else ""  # noqa: E731
    today, depts = _today(), {}
    tracks_absence = "absent" in col or "absent_until" in col
    for n, r in enumerate(rows[1:], start=2):
        name, email = cell(r, "name"), cell(r, "email").lower()
        if not name:
            snap.warnings.append(f"Строка {n}: пустое ФИО - пропущена")
            continue
        dep = cell(r, "dept")
        if dep:
            depts.setdefault(dep.lower(), Dept(dep.lower(), dep))
        absent = None
        if tracks_absence:
            until = _excel_date(cell(r, "absent_until"))
            absent = cell(r, "absent").lower() in YES or bool(until and until >= today)
        p = Person(ext=cell(r, "ext") or email or name.lower(), name=name, email=email, position=cell(r, "position"),
                   dept=dep.lower() if dep else None, active=cell(r, "fired").lower() not in YES, absent=absent)
        snap.people.append(p)
        if dep and cell(r, "head").lower() in YES:
            depts[dep.lower()].head = p.ext
    snap.depts = list(depts.values())
    return snap


# ---------- сверка с базой ----------

def _ext(obj, source: str) -> str | None:
    return (obj.ext_ids or {}).get(source)


def _link(obj, source: str, ext: str) -> None:
    if _ext(obj, source) != ext:
        obj.ext_ids = {**(obj.ext_ids or {}), source: ext}  # новый dict, чтобы SQLAlchemy заметил изменение JSON


def apply(db: Session, snap: Snapshot, actor: User | None = None, dry_run: bool = True) -> dict:
    src = snap.source
    if not snap.people:
        raise SyncError(f"{SOURCES[src]} не вернул ни одного сотрудника - ничего не меняем")
    stats = dict.fromkeys(("deps_created", "deps_updated", "created", "updated", "deactivated", "absent_on",
                           "absent_off", "skipped"), 0)
    changes: list[str] = []
    warnings = list(snap.warnings)

    # подразделения
    all_deps = list(db.scalars(select(Department)))
    by_ext = {_ext(d, src): d for d in all_deps if _ext(d, src)}
    by_name = {d.name.lower(): d for d in all_deps}
    dep_map: dict[str, Department] = {}
    for sd in snap.depts:
        d = by_ext.get(sd.ext) or by_name.get(sd.name.lower())
        if not d:
            d = Department(name=sd.name, ext_ids={src: sd.ext})
            db.add(d)
            by_name[sd.name.lower()] = d
            stats["deps_created"] += 1
            changes.append(f"Новое подразделение: {sd.name}")
        elif d.name != sd.name:
            if sd.name.lower() in by_name and by_name[sd.name.lower()] is not d:
                warnings.append(f"Подразделение «{d.name}» не переименовано: «{sd.name}» уже есть")
            else:
                changes.append(f"Подразделение «{d.name}» переименовано в «{sd.name}»")
                d.name = sd.name
                stats["deps_updated"] += 1
        _link(d, src, sd.ext)
        dep_map[sd.ext] = d

    # сотрудники
    users = list(db.scalars(select(User)))
    u_ext = {_ext(u, src): u for u in users if _ext(u, src)}
    u_mail = {u.email.lower(): u for u in users}
    seen: set[int] = set()
    person_map: dict[str, User] = {}
    to_deactivate: list[tuple[User, str]] = []
    for sp in snap.people:
        email = sp.email.strip().lower()
        u = u_ext.get(sp.ext) or (u_mail.get(email) if email else None)
        dep = dep_map.get(sp.dept) if sp.dept else None
        if not u:
            if not sp.active:
                continue  # уволен раньше, чем попал к нам
            if not email or "@" not in email:
                stats["skipped"] += 1
                warnings.append(f"{sp.name}: нет почты - не заведен")
                continue
            u = User(name=sp.name, email=email, password_hash=hash_password(secrets.token_hex(16)), role="employee",
                     position=sp.position, department=dep, is_absent=bool(sp.absent), ext_ids={src: sp.ext})
            db.add(u)
            u_mail[email] = u
            stats["created"] += 1
            changes.append(f"Новый сотрудник: {sp.name}{', ' + sp.position if sp.position else ''}"
                           f"{' (' + dep.name + ')' if dep else ''}")
            person_map[sp.ext] = u
            continue
        if u.id in seen:
            warnings.append(f"{sp.name}: встречается в источнике дважды - взята первая запись")
            continue
        seen.add(u.id)
        person_map[sp.ext] = u
        _link(u, src, sp.ext)
        diff = []
        if sp.name and u.name != sp.name:
            diff.append(f"ФИО «{u.name}» → «{sp.name}»")
            u.name = sp.name
        if sp.position and u.position != sp.position:
            diff.append(f"должность «{u.position or '-'}» → «{sp.position}»")
            u.position = sp.position
        if dep and u.department is not dep:
            diff.append(f"подразделение «{u.department.name if u.department else '-'}» → «{dep.name}»")
            u.department = dep
        if diff:
            stats["updated"] += 1
            changes.append(f"{u.name}: {'; '.join(diff)}")
        if not sp.active:
            if u.active:
                to_deactivate.append((u, "увольнение"))
            continue
        if not u.active:
            warnings.append(f"{u.name}: в источнике работает, у нас отключен - оставили как есть")
        if sp.absent is not None and u.is_absent != sp.absent:
            u.is_absent = sp.absent
            if sp.absent:
                stats["absent_on"] += 1
                to = (f"заместителю ({u.deputy.name})" if u.deputy else "руководителю подразделения"
                      if u.department and u.department.head_id and u.department.head_id != u.id else "администратору")
                changes.append(f"{u.name}: отсутствует - новые обращения уйдут {to}")
            else:
                stats["absent_off"] += 1
                changes.append(f"{u.name}: снова на месте, обращения приходят напрямую")

    # кто был связан с этим источником, но из него пропал. Файл бывает частичным (только отпуска одного отдела),
    # поэтому по файлу отключаем только тех, у кого явно стоит «Уволен»
    for u in users:
        if src != "file" and _ext(u, src) and u.id not in seen and u.active:
            to_deactivate.append((u, f"нет в источнике «{SOURCES[src]}»"))
    linked_active = sum(1 for u in users if u.active and (_ext(u, src) or u.id in seen))
    if len(to_deactivate) > max(3, linked_active * 0.3):
        db.rollback()
        raise SyncError(f"Отключились бы {len(to_deactivate)} из {linked_active} сотрудников - похоже на неполную "
                        "выгрузку. Ничего не изменено")
    for u, why in to_deactivate:
        if u.role == "admin" or (actor and u.id == actor.id):
            warnings.append(f"{u.name}: {why}, но это администратор - не отключаем")
            continue
        u.active = False
        stats["deactivated"] += 1
        changes.append(f"{u.name}: {why} - доступ отключен")

    # руководители подразделений
    for sd in snap.depts:
        d, head = dep_map.get(sd.ext), person_map.get(sd.head) if sd.head else None
        if not d or not head:
            continue
        if head.id is None:
            db.flush()
        if d.head_id != head.id:
            d.head_id = head.id
            changes.append(f"Руководитель «{d.name}»: {head.name}")
            stats["deps_updated"] += 1
        if head.role == "employee":
            head.role = "manager"
            changes.append(f"{head.name}: роль «Руководитель» - руководит подразделением")

    db.flush()
    report = {"source": src, "label": SOURCES[src], "emulator": snap.emulator, "dry_run": dry_run, "stats": stats,
              "changes": changes, "warnings": warnings, "people": len(snap.people), "departments": len(snap.depts)}
    if dry_run:
        db.rollback()
    else:
        db.add(OrgSync(source=src, actor_id=actor.id if actor else None, stats=stats, changes=changes[:300]))
        db.commit()
    return report


def fetch(source: str) -> Snapshot:
    if source == "1c":
        return fetch_1c()
    if source == "b24":
        return fetch_b24()
    raise SyncError("Неизвестный источник")


def run(db: Session, source: str, actor: User | None = None, dry_run: bool = True) -> dict:
    try:
        return apply(db, fetch(source), actor, dry_run)
    except SyncError as e:
        if not dry_run:
            db.rollback()
            db.add(OrgSync(source=source, actor_id=actor.id if actor else None, ok=False, error=str(e)))
            db.commit()
        raise


def status() -> dict:
    return {"1c": {"configured": bool(settings.onec_odata_url), "url": settings.onec_odata_url},
            "b24": {"configured": bool(settings.bitrix_webhook),
                    # ключ вебхука - секрет, наружу отдаем только портал
                    "url": re.sub(r"(/rest/\d+/).+", r"\1***", settings.bitrix_webhook)},
            "auto": {"source": settings.org_sync_source, "hours": settings.org_sync_hours}}


def maybe_auto(db: Session) -> None:
    """Синхронизация по расписанию (ORG_SYNC_SOURCE + ORG_SYNC_HOURS) из фонового цикла."""
    src = settings.org_sync_source
    if src not in ("1c", "b24") or settings.org_sync_hours <= 0:
        return
    last = db.scalar(select(OrgSync).where(OrgSync.source == src, OrgSync.actor_id.is_(None))
                     .order_by(OrgSync.id.desc()).limit(1))
    if last and last.created_at > now() - timedelta(hours=settings.org_sync_hours):
        return
    try:
        r = run(db, src, dry_run=False)
        log.info("Оргструктура из %s: %s", SOURCES[src], r["stats"])
    except SyncError as e:
        log.warning("Оргструктура из %s: %s", SOURCES[src], e)
