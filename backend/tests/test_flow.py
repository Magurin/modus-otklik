"""Сквозные сценарии из датасета организаторов (раздел 4) на чистой базе, без сети и ИИ-ключей.

Запуск:  python -m pytest tests -q   (из папки backend)
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="modus-test-")
os.environ.update(AI_MODE="rules", BACKGROUND="0", TELEGRAM_BOT_TOKEN="", MAX_BOT_TOKEN="", IMAP_HOST="")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, key):
    r = client.post("/api/auth/demo-login", json={"email": f"{key}@modus.demo"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_password_login(client):
    assert client.post("/api/auth/login", json={"email": "sales@modus.demo", "password": "demo"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "sales@modus.demo", "password": "x"}).status_code == 401
    assert client.get("/api/appeals").status_code == 401


def test_seed_matches_dataset(client):
    h = login(client, "director")
    rows = client.get("/api/appeals?scope=all", headers=h).json()
    assert len(rows) == 20
    by = {r["number"]: r for r in rows}
    assert by["A-017"]["status"] == "rejected" and by["A-004"]["status"] == "done"
    assert by["A-003"]["assignee"]["name"] == "Тимур Жумабаев"
    assert by["A-003"]["overdue"] is True


def test_create_route_and_track(client):
    emp = login(client, "sales")
    text = "В договорах с подрядчиками часто нет приложения со спецификацией, приходится запрашивать повторно."
    hint = client.post("/api/appeals/assist", json={"text": text}, headers=emp).json()
    assert hint["type"] in ("problem", "proposal") and hint["category_id"]
    cats = {c["name"]: c["id"] for c in client.get("/api/meta", headers=emp).json()["categories"]}
    r = client.post("/api/appeals", data={"text": text, "type": "problem", "category_id": cats["Договорная работа"],
                                          "priority": "medium", "title": "Нет спецификации в договорах"},
                    files=[("files", ("пример.txt", b"demo", "text/plain"))], headers=emp)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["assignee"]["name"] == "Алия Сарсенова" and a["status"] == "new" and len(a["attachments"]) == 1
    aid = a["id"]

    # посторонний сотрудник обращение не видит
    assert client.get(f"/api/appeals/{aid}", headers=login(client, "office")).status_code == 404
    # чужой руководитель не может его вести
    assert client.post(f"/api/appeals/{aid}/status", json={"status": "review"}, headers=login(client, "it")).status_code in (403, 404)

    mgr = login(client, "contracts")
    assert any(r["id"] == aid for r in client.get("/api/appeals?scope=inbox", headers=mgr).json())
    # уточнение -> ответ автора
    d = client.post(f"/api/appeals/{aid}/clarify", json={"text": "О каких подрядчиках речь?"}, headers=mgr).json()
    assert d["status"] == "clarify"
    mine = client.get(f"/api/appeals/{aid}", headers=emp).json()
    assert mine["can"]["answer"] and mine["open_question"]["text"].startswith("О каких")
    d = client.post(f"/api/appeals/{aid}/answer", json={"text": "Монтажные организации по объектам в Астане."}, headers=emp).json()
    assert d["status"] == "review"
    # внутренний комментарий автору не виден
    client.post(f"/api/appeals/{aid}/comment", json={"text": "Обсудить с юристом", "internal": True}, headers=mgr)
    assert "Обсудить с юристом" not in str(client.get(f"/api/appeals/{aid}", headers=emp).json()["events"])
    # отказ без причины невозможен, с причиной - автор ее видит
    assert client.post(f"/api/appeals/{aid}/status", json={"status": "rejected"}, headers=mgr).status_code == 400
    client.post(f"/api/appeals/{aid}/status", json={"status": "in_progress"}, headers=mgr)
    d = client.post(f"/api/appeals/{aid}/status", json={"status": "done", "text": "Спецификация стала обязательным приложением."}, headers=mgr).json()
    assert d["status"] == "done"
    mine = client.get(f"/api/appeals/{aid}", headers=emp).json()
    assert mine["resolution"].startswith("Спецификация") and mine["first_response_at"]
    notes = client.get("/api/notifications", headers=emp).json()
    assert notes["unread"] >= 3
    assert any(i["title"] == "Нет спецификации в договорах" for i in client.get("/api/improvements", headers=emp).json())


def test_absent_responsible_goes_to_deputy(client):
    adm = login(client, "admin")
    users = {u["email"]: u for u in client.get("/api/admin/users", headers=adm).json()}
    nomen = users["nomen@modus.demo"]
    client.patch(f"/api/admin/users/{nomen['id']}", json={"is_absent": True}, headers=adm)
    emp = login(client, "design")
    cats = {c["name"]: c["id"] for c in client.get("/api/meta", headers=emp).json()["categories"]}
    a = client.post("/api/appeals", data={"text": "В карточке изделия указан устаревший артикул, спецификация не сходится.",
                                          "type": "problem", "category_id": cats["Каталог и данные"], "priority": "medium",
                                          "title": "Устаревший артикул"}, headers=emp).json()
    assert a["assignee"]["name"] == "Данияр Ахметов"
    assert "отсутствует" in " ".join(e["text"] for e in a["events"])
    client.patch(f"/api/admin/users/{nomen['id']}", json={"is_absent": False}, headers=adm)


def test_anonymous_author_is_hidden(client):
    emp = login(client, "office")
    a = client.post("/api/appeals", data={"text": "Руководитель часто переносит согласования в последний момент, из-за этого срываются сроки.",
                                          "anonymous": "true"}, headers=emp).json()
    seen = client.get(f"/api/appeals/{a['id']}", headers=login(client, "director")).json()
    assert seen["author"] == {"name": "Анонимно", "anonymous": True} and seen["department"] is None
    assert "Мария" not in str(seen)
    assert client.get(f"/api/appeals/{a['id']}", headers=emp).json()["mine"] is True


def test_filters_and_analytics(client):
    h = login(client, "director")
    assert all(r["overdue"] for r in client.get("/api/appeals?scope=all&overdue=true", headers=h).json())
    assert all(r["status"] == "review" for r in client.get("/api/appeals?scope=all&status=review", headers=h).json())
    assert client.get("/api/appeals?scope=all&q=спецификац", headers=h).json()
    an = client.get("/api/analytics", headers=h).json()
    assert an["total"] >= 20 and an["avg_first_response_h"] and an["themes"] and an["overdue"]
    assert client.get("/api/analytics", headers=login(client, "sales")).status_code == 403
    dg = client.get("/api/analytics/digest", headers=h).json()
    assert "A-003" in dg["text"]


def test_support_instead_of_duplicate(client):
    emp = login(client, "office")
    hint = client.post("/api/appeals/assist", json={"text": "Обсуждения по задаче идут в разных каналах и мессенджерах, контекст теряется, нужно единое место для решений."}, headers=emp).json()
    top = hint["similar"][0]
    assert top["number"] == "A-012"
    r = client.post(f"/api/appeals/{top['id']}/support", headers=emp)
    assert r.status_code == 200 and r.json()["supports"] == 4
    assert client.post(f"/api/appeals/{top['id']}/support", headers=emp).status_code == 400


def test_meeting_to_appeals(client):
    mgr = login(client, "napr")
    text = ("[00:00] Коллеги, начнем планерку.\n[00:10] У нас проблема: заявки от продаж приходят без сроков поставки, закупки каждый раз переспрашивают.\n"
            "[00:30] Предлагаю добавить в заявку обязательное поле со сроком и объектом.\n[00:50] Решили: Данияр готовит шаблон заявки к пятнице.\n")
    m = client.post("/api/meetings", data={"title": "Планерка закупок", "transcript": text}, headers=mgr).json()
    import time
    for _ in range(50):
        d = client.get(f"/api/meetings/{m['id']}", headers=mgr).json()
        if d["status"] in ("ready", "error"):
            break
        time.sleep(0.1)
    assert d["status"] == "ready", d
    kinds = {i["kind"] for i in d["items"]}
    assert {"problem", "proposal", "decision"} <= kinds
    item = next(i for i in d["items"] if i["kind"] == "problem")
    done = client.post(f"/api/meetings/items/{item['id']}/accept", json={}, headers=mgr).json()
    assert done["appeal"]["number"].startswith("A-")
    a = client.get(f"/api/appeals/{done['appeal']['id']}", headers=login(client, "director")).json()
    assert a["source"] == "meeting" and a["author"]["external"] and a["assignee"]


def test_messenger_dialog(client):
    """Диалог с ботом одинаков для Telegram и MAX; проверяем на MAX без сети, подменив отправку."""
    from app import bots
    ch = bots.channels()["max"]
    sent = []
    ch.push = lambda chat_id, text: sent.append(text)
    ch.state.update(enabled=True, username="modus_otklik_bot")
    emp = login(client, "pm")

    bots.handle(ch, bots.Incoming(chat_id="777", text="Здравствуйте"))
    assert "для сотрудников" in sent[-1]
    link = client.post("/api/me/messengers/max/code", headers=emp).json()
    assert link["link"] == f"https://max.ru/modus_otklik_bot?start={link['code']}"
    bots.handle(ch, bots.Incoming(chat_id="777", text="/start", start_code=link["code"]))
    assert "MAX привязан" in sent[-1]
    assert next(m for m in client.get("/api/me/messengers", headers=emp).json() if m["key"] == "max")["linked"]

    bots.handle(ch, bots.Incoming(chat_id="777", text="коротко"))
    assert "подробнее" in sent[-1]
    bots.handle(ch, bots.Incoming(chat_id="777", text="На объекте не хватает актуальных схем подключения, монтажники звонят проектировщикам по каждому вопросу."))
    assert "Зарегистрировано: A-" in sent[-1]
    mine = client.get("/api/appeals?scope=mine", headers=emp).json()
    a = next(r for r in mine if r["source"] == "max")

    boss = login(client, "director")
    client.post(f"/api/appeals/{a['id']}/clarify", json={"text": "О каком объекте речь?"}, headers=boss)
    assert "О каком объекте" in sent[-1]                      # вопрос пришел в мессенджер
    bots.handle(ch, bots.Incoming(chat_id="777", text="Склад в Павлодаре"))
    assert "передан ответственному" in sent[-1]
    d = client.get(f"/api/appeals/{a['id']}", headers=boss).json()
    assert d["status"] == "review" and any(e["kind"] == "answer" and "Павлодар" in e["text"] for e in d["events"])

    bots.handle(ch, bots.Incoming(chat_id="777", text="/my"))
    assert a["number"] in sent[-1]
    client.post("/api/me/messengers/max/unlink", headers=emp)
    ch.state.update(enabled=False)


def test_email_intake(client):
    adm = login(client, "admin")
    r = client.post("/api/admin/intake/email", json={"sender": "Жанна <acc@modus.demo>", "subject": "Re: Акты сверки",
        "body": "Добрый день. Акты сверки приходят в разных форматах, приходится сводить вручную.\n\nС уважением, Жанна\n> старая переписка"}, headers=adm)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["source"] == "email" and a["author"]["name"] == "Жанна Мухамедова"
    full = client.get(f"/api/appeals/{a['id']}", headers=adm).json()
    assert "старая переписка" not in full["text"] and "С уважением" not in full["text"]
