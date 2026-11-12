"""Google Календарь (на подмененном транспорте, без сети) и MCP-сервер для ИИ-агентов."""
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import gcal
from app.config import settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, key):
    r = client.post("/api/auth/demo-login", json={"email": f"{key}@modus.demo"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


class FakeGoogle:
    """Минимальный Google: выдача токенов и события основного календаря."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[str] = []
        self.n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(f"{req.method} {path}")
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer at"
        event_id = path.rsplit("/", 1)[-1] if not path.endswith("/events") else ""
        if req.method == "POST":
            self.n += 1
            self.events[f"ev{self.n}"] = httpx.Response(200, content=req.content).json()
            return httpx.Response(200, json={"id": f"ev{self.n}"})
        if event_id not in self.events:
            return httpx.Response(404, json={})
        if req.method == "PATCH":
            self.events[event_id] = httpx.Response(200, content=req.content).json()
            return httpx.Response(200, json={"id": event_id})
        del self.events[event_id]
        return httpx.Response(204)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(settings, "google_client_id", "client-id", raising=False)
    monkeypatch.setattr(settings, "google_client_secret", "secret", raising=False)
    monkeypatch.setattr(gcal, "_http", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    gcal._tokens.clear()
    return fake


def new_appeal(client, emp, title):
    cats = {c["name"]: c["id"] for c in client.get("/api/meta", headers=emp).json()["categories"]}
    r = client.post("/api/appeals", data={"text": f"{title}: в договоре с подрядчиком не указаны сроки поставки, уточняем по телефону.",
                                          "type": "problem", "category_id": cats["Договорная работа"], "priority": "medium",
                                          "title": title}, headers=emp)
    assert r.status_code == 200, r.text
    return r.json()


def test_calendar_link_without_setup(client):
    emp = login(client, "sales")
    a = new_appeal(client, emp, "Сроки поставки в договоре")
    assert client.get("/api/me/calendar", headers=emp).json() == {"configured": False, "connected": False, "email": ""}
    assert client.post("/api/me/calendar/connect", headers=emp).status_code == 400
    q = parse_qs(urlparse(a["calendar_url"]).query)
    start, end = q["dates"][0].split("/")
    days = (date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}") - date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:]}")).days
    sla = next(c["sla_days"] for c in client.get("/api/meta", headers=emp).json()["categories"] if c["name"] == "Договорная работа")
    assert days == sla + 1 and a["number"] in q["text"][0] and a["in_calendar"] is False


def test_calendar_follows_the_appeal(client, google):
    mgr, emp = login(client, "contracts"), login(client, "sales")
    url = client.post("/api/me/calendar/connect", headers=mgr).json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"] and q["redirect_uri"][0].endswith("/api/calendar/google/callback")
    # чужая или испорченная подпись не подключает календарь
    r = client.get("/api/calendar/google/callback", params={"code": "c", "state": "x"}, follow_redirects=False)
    assert r.headers["location"].endswith("calendar=denied")
    r = client.get("/api/calendar/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert r.headers["location"].endswith("calendar=ok")
    assert client.get("/api/me/calendar", headers=mgr).json()["connected"] is True
    inbox = [x for x in client.get("/api/appeals?scope=inbox&status=open", headers=mgr).json()]
    assert len(google.events) >= len(inbox) > 0  # открытые обращения разложены по календарю при подключении

    a = new_appeal(client, emp, "Нет сроков поставки")
    mine = {k: e for k, e in google.events.items() if e["summary"].startswith(a["number"])}
    assert len(mine) == 1
    (eid, ev), = mine.items()
    span = (date.fromisoformat(ev["end"]["date"]) - date.fromisoformat(ev["start"]["date"])).days
    sla = next(c["sla_days"] for c in client.get("/api/meta", headers=emp).json()["categories"] if c["name"] == "Договорная работа")
    assert span == sla + 1 and "Вы отвечаете" in ev["description"]  # весь срок; конец в Google не включительно
    assert client.get(f"/api/appeals/{a['id']}", headers=mgr).json()["in_calendar"] is True

    # комментарий ничего в календаре не меняет - Google лишний раз не дергаем
    before = len(google.calls)
    client.post(f"/api/appeals/{a['id']}/comment", json={"text": "Посмотрю завтра"}, headers=mgr)
    assert len(google.calls) == before

    # перенос срока удлиняет ту же полосу
    client.patch(f"/api/appeals/{a['id']}", json={"due_days": 20}, headers=mgr)
    ev = google.events[eid]
    assert date.fromisoformat(ev["end"]["date"]) - date.fromisoformat(ev["start"]["date"]) == timedelta(days=21)

    # закрыли - событие отмечено и заканчивается днем закрытия
    client.post(f"/api/appeals/{a['id']}/status", json={"status": "in_progress"}, headers=mgr)
    r = client.post(f"/api/appeals/{a['id']}/status", json={"status": "done", "text": "Срок поставки добавлен в шаблон"}, headers=mgr)
    assert r.status_code == 200, r.text
    ev = google.events[eid]
    assert ev["summary"].startswith("✓ ") and ev["end"]["date"] == (date.fromisoformat(ev["start"]["date"]) + timedelta(days=1)).isoformat()

    # передали другому - событие из календаря прежнего ответственного убрано
    b = new_appeal(client, emp, "Нет приложения к договору")
    eid_b = next(k for k, e in google.events.items() if e["summary"].startswith(b["number"]))
    users = {u["email"]: u for u in client.get("/api/admin/users", headers=login(client, "admin")).json()}
    client.post(f"/api/appeals/{b['id']}/assign", json={"user_id": users["it@modus.demo"]["id"]}, headers=mgr)
    assert eid_b not in google.events

    assert client.post("/api/me/calendar/disconnect", headers=mgr).json()["connected"] is False
    assert google.events == {}


def rpc(client, method, params=None, key="", id=1, **kw):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        body["id"] = id
    return client.post("/api/mcp", json=body, headers={"Authorization": f"Bearer {key}"} if key else {}, **kw)


def call(client, key, name, args=None):
    r = rpc(client, "tools/call", {"name": name, "arguments": args or {}}, key).json()["result"]
    return r.get("structuredContent"), r


def test_agent_discusses_then_submits(client):
    emp = login(client, "sales")
    assert rpc(client, "initialize").status_code == 401
    assert rpc(client, "initialize", key="mok_wrong").status_code == 401
    assert client.get("/api/me/agent", headers=emp).json()["enabled"] is False
    got = client.post("/api/me/agent/key", headers=emp).json()
    key = got["key"]
    assert key.startswith("mok_") and got["url"].endswith("/api/mcp")
    # навык без ключа - его можно пересылать и хранить; ключ есть только в промпте подключения сразу после выдачи
    assert key not in got["skill"] and "name: modus-otklik" in got["skill"] and "submit_task" in got["skill"]
    assert key in got["prompt"] and got["skill"] in got["prompt"]
    later = client.get("/api/me/agent", headers=emp).json()
    assert later["enabled"] and "key" not in later and key not in later["prompt"] and "MODUS_OTKLIK_KEY" in later["prompt"]

    init = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, key).json()
    assert init["id"] == 1 and init["result"]["protocolVersion"] == "2025-06-18" and "submit_task" in init["result"]["instructions"]
    assert rpc(client, "initialize", {"protocolVersion": "1999-01-01"}, key).json()["result"]["protocolVersion"] == "2025-11-25"
    assert rpc(client, "notifications/initialized", key=key, id=None).status_code == 202
    assert client.get("/api/mcp").status_code == 405
    tools = {t["name"]: t for t in rpc(client, "tools/list", key=key).json()["result"]["tools"]}
    assert {"get_guide", "draft_task", "submit_task", "find_similar", "list_tasks", "get_task", "support_task"} == set(tools)
    assert tools["draft_task"]["annotations"]["readOnlyHint"] and tools["submit_task"]["inputSchema"]["required"] == ["text"]
    # ключ в адресе - для клиентов, которые не передают заголовки
    assert client.post(f"/api/mcp/{key}", json={"jsonrpc": "2.0", "id": 7, "method": "ping"}).json() == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert rpc(client, "resources/list", key=key).json()["error"]["code"] == -32601

    guide, _ = call(client, key, "get_guide")
    assert guide["me"]["role"] == "Сотрудник" and any(c["name"] == "Договорная работа" for c in guide["categories"])

    text = "Склад принимает заявки на отгрузку только по телефону, заявки теряются, клиенты ждут лишние дни."
    before = len(client.get("/api/appeals?scope=all", headers=login(client, "director")).json())
    draft, _ = call(client, key, "draft_task", {"text": text})
    assert draft["type"] in ("problem", "proposal") and draft["assignee"] and draft["sla_days"] > 0
    assert len(client.get("/api/appeals?scope=all", headers=login(client, "director")).json()) == before  # черновик ничего не создал

    _, bad = call(client, key, "submit_task", {"text": text, "category": "Нет такой"})
    assert bad["isError"] and "Договорная работа" in bad["content"][0]["text"]
    done, raw = call(client, key, "submit_task", {"text": text, "title": "Заявки на отгрузку теряются", "type": "problem",
                                                  "category": "договорная работа", "solution": "Форма заявки в общей таблице"})
    assert done["number"].startswith("A-") and done["assignee"] == "Алия Сарсенова" and "calendar.google.com" in done["calendar_url"]
    assert done["number"] in raw["content"][0]["text"]

    mine = client.get("/api/appeals?scope=mine", headers=emp).json()[0]
    assert mine["number"] == done["number"] and mine["source"] == "agent" and mine["title"] == "Заявки на отгрузку теряются"
    listed, _ = call(client, key, "list_tasks", {"scope": "mine", "status": "open"})
    assert listed["tasks"][0]["number"] == done["number"]
    task, _ = call(client, key, "get_task", {"number": done["number"].lower()})
    assert task["text"] == text and task["solution"].startswith("Форма")

    # чужое обращение по номеру не читается, но присоединиться к нему можно; к своему - нельзя
    other = new_appeal(client, login(client, "office"), "Долго согласуют договоры")
    _, r = call(client, key, "get_task", {"number": other["number"]})
    assert r["isError"]
    joined, _ = call(client, key, "support_task", {"number": other["number"]})
    assert joined["supports"] == 1
    assert call(client, key, "support_task", {"number": done["number"]})[1]["isError"]

    # новый ключ отменяет прежний, отзыв закрывает доступ
    key2 = client.post("/api/me/agent/key", headers=emp).json()["key"]
    assert rpc(client, "ping", key=key).status_code == 401 and rpc(client, "ping", key=key2).status_code == 200
    client.post("/api/me/agent/revoke", headers=emp)
    assert rpc(client, "ping", key=key2).status_code == 401


def test_links_follow_request_address(client):
    """APP_URL не задан - ссылки ведут туда, откуда пришел запрос (например, фронтенд на другом порту через прокси)."""
    emp = login(client, "sales")
    via = {**emp, "X-Forwarded-Host": "127.0.0.1:5183", "X-Forwarded-Proto": "http"}
    a = client.get("/api/me/agent", headers=via).json()
    assert a["url"] == "http://127.0.0.1:5183/api/mcp" and "http://127.0.0.1:5183/api/mcp" in a["skill"]
    assert client.get("/api/me/agent", headers=emp).json()["url"] == "http://testserver/api/mcp"
    key = client.post("/api/me/agent/key", headers=emp).json()["key"]
    task = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "list_tasks", "arguments": {"scope": "mine"}}},
        headers={"Authorization": f"Bearer {key}", "Host": "127.0.0.1:5183"}).json()["result"]["structuredContent"]["tasks"][0]
    assert task["url"].startswith("http://127.0.0.1:5183/appeals/")
