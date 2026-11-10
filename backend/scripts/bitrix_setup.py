"""Заполнить тестовый портал Битрикс24 демо-оргструктурой из seed.py через входящий вебхук.

Создает подразделения, заводит сотрудников (почта @modus.demo - такого домена нет, приглашения никуда не уходят)
и назначает руководителей. Повторный запуск ничего не дублирует: существующее находится по названию и почте.
По умолчанию только показывает план; изменения - с флагом --apply.

Вебхук: Битрикс24 -> Разработчикам -> Другое -> Входящий вебхук, права: user, department.

    cd backend
    .venv/Scripts/python -m scripts.bitrix_setup https://<портал>.bitrix24.ru/rest/1/<ключ>
    .venv/Scripts/python -m scripts.bitrix_setup https://<портал>.bitrix24.ru/rest/1/<ключ> --apply
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

from app.seed import DEPARTMENTS, USERS


class Portal:
    def __init__(self, base: str, client: httpx.Client | None = None):
        self.base = base.rstrip("/")
        self.http = client or httpx.Client(timeout=30)

    def call(self, method: str, params: dict | None = None) -> dict:
        r = self.http.post(f"{self.base}/{method}.json", data=params or {})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or "error" in data:
            raise SystemExit(f"{method}: {data.get('error_description') or data.get('error') or r.status_code}")
        time.sleep(0.5)  # лимит REST - около 2 запросов в секунду
        return data

    def all(self, method: str, params: dict | None = None) -> list[dict]:
        rows, start = [], 0
        while True:
            data = self.call(method, {**(params or {}), "start": start})
            rows += data.get("result") or []
            if "next" not in data:
                return rows
            start = data["next"]


def flat(prefix: str, value) -> dict:
    """{"UF_DEPARTMENT": [3]} -> {"UF_DEPARTMENT[0]": 3} - так REST принимает массивы в форме."""
    return {f"{prefix}[{i}]": v for i, v in enumerate(value)}


def setup(portal: Portal, apply: bool, out=print) -> None:
    deps = {d["NAME"].strip().lower(): d for d in portal.all("department.get")}
    root = min(deps.values(), key=lambda d: int(d["ID"]))  # корневое подразделение - компания
    dep_id: dict[str, str] = {}
    for name in DEPARTMENTS:
        d = deps.get(name.lower())
        if d:
            dep_id[name] = str(d["ID"])
            continue
        out(f"+ подразделение «{name}»")
        dep_id[name] = str(portal.call("department.add", {"NAME": name, "PARENT": root["ID"]})["result"]) if apply else "new"

    users = {(u.get("EMAIL") or "").lower(): u for u in portal.all("user.get", {"FILTER[USER_TYPE]": "employee"})}
    user_id: dict[str, str] = {}
    for key, (name, position, _role, dep, _head) in USERS.items():
        email = f"{key}@modus.demo"
        u = users.get(email)
        if u:
            user_id[key] = str(u["ID"])
            continue
        out(f"+ сотрудник {name} <{email}>, {position}, {dep}")
        if apply:
            first, _, last = name.partition(" ")
            params = {"NAME": first, "LAST_NAME": last, "EMAIL": email, "WORK_POSITION": position,
                      "EXTRANET": "N", **flat("UF_DEPARTMENT", [dep_id[dep]])}
            user_id[key] = str(portal.call("user.add", params)["result"])
        else:
            user_id[key] = "new"

    for key, (name, _p, _r, dep, head) in USERS.items():
        if not head or dep not in dep_id or key not in user_id:
            continue
        current = (deps.get(dep.lower()) or {}).get("UF_HEAD")
        if str(current or "") == user_id[key]:
            continue
        out(f"* руководитель «{dep}»: {name}")
        if apply:
            portal.call("department.update", {"ID": dep_id[dep], "UF_HEAD": user_id[key]})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("webhook", help="адрес входящего вебхука: https://<портал>/rest/<id>/<ключ>")
    ap.add_argument("--apply", action="store_true", help="внести изменения (без флага - только план)")
    args = ap.parse_args()
    lines: list[str] = []
    setup(Portal(args.webhook), args.apply, out=lambda s: (lines.append(s), print(s)))
    if not lines:
        print("Портал уже заполнен, менять нечего")
    elif not args.apply:
        print("\nЭто план. Чтобы внести изменения, добавьте --apply")


if __name__ == "__main__":
    sys.exit(main())
