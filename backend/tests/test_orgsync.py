"""Оргструктура из 1С, Битрикс24 и файла - на встроенных эмуляторах, без сети.

Файл идет после test_flow.py и использует ту же базу, поэтому в конце возвращает демо-данные как были.
"""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="modus-test-"))
os.environ.update(AI_MODE="rules", BACKGROUND="0", TELEGRAM_BOT_TOKEN="", IMAP_HOST="",
                  ONEC_ODATA_URL="", BITRIX_WEBHOOK_URL="")

import pytest
from fastapi.testclient import TestClient

from app import orgsync
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, key):
    r = client.post("/api/auth/demo-login", json={"email": f"{key}@modus.demo"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def users(client, h):
    return {u["email"]: u for u in client.get("/api/admin/users", headers=h).json()}


def test_1c_preview_changes_nothing(client):
    h = login(client, "admin")
    before = users(client, h)
    r = client.post("/api/admin/org/sync/1c", headers=h)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["dry_run"] and rep["emulator"]
    assert rep["stats"]["created"] == 2 and rep["stats"]["absent_on"] == 1 and rep["stats"]["deactivated"] == 0
    assert any("Тимур Жумабаев" in c and "Данияр Ахметов" in c for c in rep["changes"])
    assert users(client, h) == before
    assert client.post("/api/admin/org/sync/1c", headers=login(client, "sales")).status_code == 403


def test_1c_apply_and_routing(client):
    h = login(client, "admin")
    rep = client.post("/api/admin/org/sync/1c?apply=true", headers=h).json()
    assert rep["stats"]["created"] == 2
    u = users(client, h)
    assert u["nomen@modus.demo"]["is_absent"] and "1c" in u["nomen@modus.demo"]["synced"]
    assert u["kamila@modus.demo"]["department"] == "Бухгалтерия" and u["kamila@modus.demo"]["role"] == "employee"
    assert "oleg@modus.demo" not in u  # уволен до того, как попал к нам
    # обращение в категорию Тимура уходит его заместителю
    cats = client.get("/api/admin/categories", headers=h).json()
    assert any(c["route"] == "Данияр Ахметов" and "Тимур Жумабаев отсутствует" in (c["route_reason"] or "") for c in cats)
    # повторный запуск ничего не меняет
    again = client.post("/api/admin/org/sync/1c?apply=true", headers=h).json()
    assert not again["changes"], again["changes"]
    state = client.get("/api/admin/org", headers=h).json()
    assert state["runs"][0]["source"] == "1c" and state["linked"]["1c"] == len(u)


def test_bitrix_pagination_and_extranet(client):
    h = login(client, "admin")
    rep = client.post("/api/admin/org/sync/b24", headers=h).json()
    # 21 из демо-базы + 2 новых; уволенного портал не отдает, экстранет отфильтрован
    assert rep["people"] == 23 and rep["departments"] == 9
    assert rep["stats"]["created"] == 0  # Илья уже заведен из 1С и сопоставлен по почте
    assert any("отпуска" in w for w in rep["warnings"])


def test_bitrix_pages(monkeypatch):
    monkeypatch.setattr(orgsync.mocks, "PAGE", 5)
    snap = orgsync.fetch_b24()
    assert len(snap.people) == 23 and len({p.ext for p in snap.people}) == 23


def test_file_import(client):
    h = login(client, "admin")
    csv = ("ФИО;Почта;Должность;Подразделение;Руководитель подразделения;Отсутствует;Уволен\n"
           "Тимур Жумабаев;nomen@modus.demo;;Закупки;;нет;\n"
           "Новый Человек;new.person@modus.demo;Аналитик;Аналитика;да;;\n"
           "Без Почты;;Курьер;Закупки;;;\n").encode("cp1251")
    r = client.post("/api/admin/org/import", data={"apply": "true"}, files={"file": ("staff.csv", csv, "text/csv")}, headers=h)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["stats"]["created"] == 1 and rep["stats"]["deps_created"] == 1 and rep["stats"]["absent_off"] == 1
    assert rep["stats"]["skipped"] == 1
    u = users(client, h)
    assert u["new.person@modus.demo"]["role"] == "manager"  # руководит новым подразделением
    assert not u["nomen@modus.demo"]["is_absent"]
    bad = client.post("/api/admin/org/import", files={"file": ("x.csv", "Имя;Телефон\nА;1\n".encode(), "text/csv")}, headers=h)
    assert bad.status_code == 400 and "Почта" in bad.json()["detail"]


def test_mass_deactivation_guard(client, monkeypatch):
    h = login(client, "admin")
    real = orgsync.mocks.onec_entity
    monkeypatch.setattr(orgsync.mocks, "onec_entity",
                        lambda n: real(n)[:3] if n == "Catalog_Сотрудники" else real(n))
    r = client.post("/api/admin/org/sync/1c?apply=true", headers=h)
    assert r.status_code == 400 and "неполную" in r.json()["detail"]
    assert users(client, h)["sales@modus.demo"]["active"]


def test_mock_http_endpoints(client):
    r = client.get("/mock/1c/odata/standard.odata/Catalog_Сотрудники?$format=json")
    assert r.status_code == 200 and len(r.json()["value"]) == 24
    r = client.post("/mock/bitrix/rest/1/demo/user.get.json", data={"FILTER[USER_TYPE]": "employee"})
    assert r.json()["total"] == 23


def test_restore_demo(client):
    h = login(client, "admin")
    nomen = users(client, h)["nomen@modus.demo"]
    client.patch(f"/api/admin/users/{nomen['id']}", json={"is_absent": False}, headers=h)
