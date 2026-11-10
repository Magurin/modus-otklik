"""Эмуляторы 1С:ЗУП (OData) и Битрикс24 (REST) для стенда без доступа к настоящим системам.

Данные - та же вымышленная оргструктура, что в seed.py, плюс несколько изменений, которые синхронизация должна
заметить: двое новых сотрудников, один в отпуске, один уволенный. Формат ответов повторяет настоящие:
- 1С: стандартный интерфейс OData (/odata/standard.odata/<Объект>?$format=json -> {"value": [...]});
- Битрикс24: входящий вебхук (/rest/<id>/<ключ>/<метод> -> {"result": [...], "total": N, "next": M}).
Код синхронизации ходит сюда тем же HTTP-клиентом, что и в настоящие системы, меняется только адрес.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request

from .models import now
from .seed import DEPARTMENTS, USERS

router = APIRouter(prefix="/mock", include_in_schema=False)


def _guid(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"modus-1c/{kind}/{key}"))


# кто есть в учетной системе сверх нашей демо-базы: (ключ, ФИО, должность, подразделение, уволен)
EXTRA = [
    ("kamila", "Камила Есенова", "Бухгалтер", "Бухгалтерия", False),
    ("ilya", "Илья Бондарев", "Системный администратор", "IT", False),
    ("oleg", "Олег Пак", "Водитель", "Административный отдел", True),
]
VACATION = "nomen"        # в отпуске сейчас - его обращения должны уйти заместителю
TRIP_LATER = "design"     # командировка в будущем - сейчас не отсутствует


def _people() -> list[dict]:
    rows = [{"key": k, "name": n, "position": p, "dep": d, "head": h, "fired": False}
            for k, (n, p, _role, d, h) in USERS.items()]
    rows += [{"key": k, "name": n, "position": p, "dep": d, "head": False, "fired": f} for k, n, p, d, f in EXTRA]
    return rows


# ---------- 1С:ЗУП, OData ----------

def _date(dt) -> str:
    return dt.strftime("%Y-%m-%dT00:00:00")


def onec_entity(name: str) -> list[dict]:
    people = _people()
    today = now()
    positions = sorted({p["position"] for p in people})
    if name == "Catalog_ПодразделенияОрганизаций":
        return [{"Ref_Key": _guid("dep", d), "Code": f"{i:05d}", "Description": d, "DeletionMark": False}
                for i, d in enumerate(DEPARTMENTS, 1)]
    if name == "Catalog_Должности":
        return [{"Ref_Key": _guid("pos", p), "Description": p, "DeletionMark": False} for p in positions]
    if name == "Catalog_ФизическиеЛица":
        return [{"Ref_Key": _guid("person", p["key"]), "Description": p["name"], "DeletionMark": False,
                 "КонтактнаяИнформация": [{"LineNumber": "1", "Тип": "АдресЭлектроннойПочты",
                                          "Представление": f"{p['key']}@modus.demo"}]} for p in people]
    if name == "Catalog_Сотрудники":
        return [{"Ref_Key": _guid("emp", p["key"]), "Code": f"{i:010d}", "Description": p["name"],
                 "ФизическоеЛицо_Key": _guid("person", p["key"]), "ВАрхиве": p["fired"], "DeletionMark": False}
                for i, p in enumerate(people, 1)]
    if name == "InformationRegister_КадроваяИсторияСотрудников":
        rows = []
        for p in people:
            hired = today - timedelta(days=30 if p["key"] in ("kamila", "ilya") else 900)
            rows.append({"Period": _date(hired), "Сотрудник_Key": _guid("emp", p["key"]), "ВидСобытия": "Прием",
                         "Подразделение_Key": _guid("dep", p["dep"]), "Должность_Key": _guid("pos", p["position"])})
            if p["fired"]:
                rows.append({"Period": _date(today - timedelta(days=10)), "Сотрудник_Key": _guid("emp", p["key"]),
                             "ВидСобытия": "Увольнение", "Подразделение_Key": _guid("dep", p["dep"]),
                             "Должность_Key": _guid("pos", p["position"])})
        return rows
    if name == "InformationRegister_РуководителиПодразделений":
        return [{"Подразделение_Key": _guid("dep", p["dep"]), "Руководитель_Key": _guid("emp", p["key"])}
                for p in people if p["head"]]
    if name == "Document_Отпуск":
        return [{"Ref_Key": _guid("doc", "vac1"), "Number": "ЗУ00-000041", "Date": _date(today - timedelta(days=5)),
                 "Posted": True, "DeletionMark": False, "Сотрудник_Key": _guid("emp", VACATION),
                 "ДатаНачалаОсновногоОтпуска": _date(today - timedelta(days=2)),
                 "ДатаОкончанияОсновногоОтпуска": _date(today + timedelta(days=12))}]
    if name == "Document_Командировка":
        return [{"Ref_Key": _guid("doc", "trip1"), "Number": "ЗУ00-000017", "Date": _date(today),
                 "Posted": True, "DeletionMark": False, "Сотрудник_Key": _guid("emp", TRIP_LATER),
                 "ДатаНачала": _date(today + timedelta(days=20)), "ДатаОкончания": _date(today + timedelta(days=24))}]
    raise KeyError(name)


@router.get("/1c/odata/standard.odata/{entity}")
def onec_odata(entity: str):
    try:
        rows = onec_entity(entity)
    except KeyError:
        raise HTTPException(404, {"odata.error": {"code": "", "message": {"lang": "ru", "value": f"Не найден объект {entity}"}}})
    return {"odata.metadata": f"standard.odata/$metadata#{entity}", "value": rows}


# ---------- Битрикс24, REST ----------

PAGE = 50


def bitrix_call(method: str, params: dict) -> dict:
    people = [p for p in _people() if not p["fired"]]  # уволенных портал отдает как ACTIVE=false, но фильтром мы их не просим
    ids = {p["key"]: i for i, p in enumerate(people, 1)}
    dep_ids = {d: i for i, d in enumerate(DEPARTMENTS, 1)}
    if method == "user.get":
        rows = []
        for p in people:
            first, _, last = p["name"].partition(" ")
            rows.append({"ID": str(ids[p["key"]]), "ACTIVE": True, "NAME": first, "LAST_NAME": last, "SECOND_NAME": "",
                         "EMAIL": f"{p['key']}@modus.demo", "WORK_POSITION": p["position"],
                         "UF_DEPARTMENT": [dep_ids[p["dep"]]], "USER_TYPE": "employee"})
        rows.append({"ID": str(len(rows) + 1), "ACTIVE": True, "NAME": "Подрядчик", "LAST_NAME": "Экстранет",
                     "EMAIL": "partner@example.com", "WORK_POSITION": "", "UF_DEPARTMENT": [], "USER_TYPE": "extranet"})
        want = params.get("FILTER[USER_TYPE]")
        rows = [r for r in rows if not want or r["USER_TYPE"] == want]
    elif method == "department.get":
        heads = {p["dep"]: ids[p["key"]] for p in people if p["head"]}
        rows = [{"ID": str(i), "NAME": d, "SORT": i * 10, "PARENT": None if i == 1 else "1",
                 "UF_HEAD": str(heads.get(d, ""))} for d, i in dep_ids.items()]
    else:
        return {"error": "ERROR_METHOD_NOT_FOUND", "error_description": "Method not found!"}
    start = int(params.get("start") or 0)
    page = rows[start:start + PAGE]
    out = {"result": page, "total": len(rows)}
    if start + PAGE < len(rows):
        out["next"] = start + PAGE
    return out


@router.api_route("/bitrix/rest/{uid}/{key}/{method}", methods=["GET", "POST"])
async def bitrix_rest(uid: str, key: str, method: str, request: Request):
    params = dict(request.query_params)
    if request.method == "POST":
        params.update(dict(await request.form()))
    return bitrix_call(method.removesuffix(".json"), params)
