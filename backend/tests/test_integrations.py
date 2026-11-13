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
    assert {"get_guide", "draft_task", "submit_task", "find_similar", "list_tasks", "get_task", "support_task",
            "get_channels", "connect_messenger", "disconnect_messenger"} == set(tools)  # совещаний и почты у сотрудника нет
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


def agent_key(client, who):
    return client.post("/api/me/agent/key", headers=login(client, who)).json()["key"]


def wait_meeting(client, key, meeting_id):
    import time
    for _ in range(100):
        m, _ = call(client, key, "get_meeting", {"meeting_id": meeting_id})
        if m["status"] in ("ready", "error"):
            return m
        time.sleep(0.1)
    raise AssertionError(m)


MEETING = ("[00:00] Коллеги, начнем планерку.\n[00:10] У нас проблема: заявки от продаж приходят без сроков поставки, "
           "закупки каждый раз переспрашивают.\n[00:30] Предлагаю добавить в заявку обязательное поле со сроком и объектом.\n"
           "[00:50] Решили: Данияр готовит шаблон заявки к пятнице.\n")


def test_agent_meetings(client):
    import base64
    emp, mgr = agent_key(client, "sales"), agent_key(client, "napr")
    # совещания - только тем, кто рассматривает обращения, как в интерфейсе
    assert "add_meeting" not in {t["name"] for t in rpc(client, "tools/list", key=emp).json()["result"]["tools"]}
    assert call(client, emp, "list_meetings")[1]["isError"]
    names = {t["name"] for t in rpc(client, "tools/list", key=mgr).json()["result"]["tools"]}
    assert {"list_meetings", "add_meeting", "get_meeting", "accept_meeting_item", "reject_meeting_item", "retry_meeting"} <= names
    assert "intake_email" not in names
    assert "add_meeting" in rpc(client, "initialize", key=mgr).json()["result"]["instructions"]
    skill = client.get("/api/me/agent", headers=login(client, "napr")).json()["skill"]
    assert "accept_meeting_item" in skill and "connect_messenger" in skill and "intake_email" not in skill

    # неверные источники
    assert call(client, mgr, "add_meeting", {"title": "Пусто"})[1]["isError"]
    assert call(client, mgr, "add_meeting", {"title": "Два", "transcript": MEETING, "file_url": "https://example.com/a.txt"})[1]["isError"]
    _, r = call(client, mgr, "add_meeting", {"title": "Внутренняя", "file_url": "http://127.0.0.1:8000/a.mp3"})
    assert r["isError"] and "внутреннюю сеть" in r["content"][0]["text"]
    assert call(client, mgr, "add_meeting", {"title": "Без имени", "file_base64": "aGVsbG8="})[1]["isError"]

    m, _ = call(client, mgr, "add_meeting", {"title": "Планерка закупок", "held_at": "2026-11-10", "transcript": MEETING})
    assert m["held_at"] == "10.11.2026"
    m = wait_meeting(client, mgr, m["meeting_id"])
    assert m["status"] == "ready", m
    assert {"problem", "proposal", "decision"} <= {i["kind"] for i in m["items"]}
    problem = next(i for i in m["items"] if i["kind"] == "problem")
    assert problem["becomes_task"] and problem["assignee"] and problem["sla_days"] > 0 and problem["state"] == "draft"
    assert "transcript" not in m and call(client, mgr, "get_meeting", {"meeting_id": m["meeting_id"], "transcript": True})[0]["transcript"] == MEETING.strip()

    # подтвердить от имени сотрудника - обращение уходит по маршруту
    assert call(client, mgr, "accept_meeting_item", {"item_id": problem["item_id"], "author": "Нет Такого"})[1]["isError"]
    done, _ = call(client, mgr, "accept_meeting_item", {"item_id": problem["item_id"], "author": "sales@modus.demo",
                                                        "priority": "high", "category": "Договорная работа"})
    assert done["state"] == "accepted" and done["task"]["number"].startswith("A-") and "calendar.google.com" in done["calendar_url"]
    assert done["task"]["priority"] == "Высокий" and done["task"]["assignee"] == "Алия Сарсенова"
    mine, _ = call(client, emp, "list_tasks", {"scope": "mine"})
    assert mine["tasks"][0]["number"] == done["task"]["number"]
    assert call(client, mgr, "accept_meeting_item", {"item_id": problem["item_id"]})[1]["isError"]  # уже обработан

    decision = next(i for i in m["items"] if i["kind"] == "decision")
    kept, _ = call(client, mgr, "accept_meeting_item", {"item_id": decision["item_id"]})
    assert kept["task"] is None and "протоколе" in kept["message"]
    proposal = next(i for i in m["items"] if i["kind"] == "proposal")
    assert call(client, mgr, "reject_meeting_item", {"item_id": proposal["item_id"]})[0]["state"] == "rejected"

    # чужое совещание руководитель не видит, директор видит
    other = agent_key(client, "it")
    assert call(client, other, "get_meeting", {"meeting_id": m["meeting_id"]})[1]["isError"]
    assert call(client, other, "accept_meeting_item", {"item_id": proposal["item_id"]})[1]["isError"]
    director = agent_key(client, "director")
    assert any(x["meeting_id"] == m["meeting_id"] for x in call(client, director, "list_meetings")[0]["meetings"])

    # файл протокола в base64 и повторный разбор
    f, _ = call(client, mgr, "add_meeting", {"title": "Протокол файлом", "filename": "протокол.txt",
                                             "file_base64": base64.b64encode(MEETING.encode("cp1251")).decode()})
    f = wait_meeting(client, mgr, f["meeting_id"])
    assert f["status"] == "ready" and f["file"] == "протокол.txt" and f["items_total"] >= 3
    again, _ = call(client, mgr, "retry_meeting", {"meeting_id": f["meeting_id"]})
    assert wait_meeting(client, mgr, again["meeting_id"])["status"] == "ready"


def test_record_split_for_speech_api(monkeypatch):
    """Запись от агента распознается на сервере кусками по 2 минуты, как в браузере, с метками времени."""
    from app import meetings
    sent = []
    monkeypatch.setattr(meetings, "_pcm", lambda path: b"\0" * (16000 * 2 * 250))  # 4 мин 10 с
    monkeypatch.setattr(meetings, "transcribe_api", lambda audio, name, prev: sent.append((len(audio), prev)) or f"кусок {len(sent)}")
    text = meetings.transcribe_api_file("x.mp3")
    assert text == "[00:00] кусок 1\n[02:00] кусок 2\n[04:00] кусок 3"
    assert all(n <= meetings.MAX_API_PART for n, _ in sent) and sent[1][1] == "[00:00] кусок 1"


def test_agent_channels(client, monkeypatch):
    from app import bots
    emp, admin = agent_key(client, "sales"), agent_key(client, "admin")
    ch, _ = call(client, emp, "get_channels")
    assert ch["messengers"] == [] and ch["email"]["address"] is None and "admin" not in ch
    assert call(client, emp, "connect_messenger", {"messenger": "telegram"})[1]["isError"]  # бот не запущен
    assert call(client, emp, "connect_messenger", {"messenger": "icq"})[1]["isError"]

    tg = bots.channels()["telegram"]
    monkeypatch.setattr(tg, "state", {"enabled": True, "username": "modus_test_bot", "error": "", "link": ""})
    link, _ = call(client, emp, "connect_messenger", {})
    assert link["bot"] == "modus_test_bot" and link["code"] in link["link"] and not link["already_linked"]
    ch, _ = call(client, emp, "get_channels")
    assert ch["messengers"][0] == {"messenger": "telegram", "title": "Telegram", "working": True, "bot": "modus_test_bot", "linked": False}
    assert call(client, emp, "disconnect_messenger", {"messenger": "telegram"})[1]["isError"]  # и так не привязан

    # почта: письмо регистрируется как обращение, автор - по адресу
    assert call(client, emp, "intake_email", {"sender": "sales@modus.demo", "body": "x" * 30})[1]["isError"]
    assert {"intake_email", "check_mailbox"} <= {t["name"] for t in rpc(client, "tools/list", key=admin).json()["result"]["tools"]}
    got, _ = call(client, admin, "intake_email", {"sender": "Артем Белов <sales@modus.demo>", "subject": "Fwd: Счета",
                                                   "body": "Счета поставщиков приходят в личную почту и теряются.\n\n-- \nАртем"})
    assert got["number"].startswith("A-") and got["author"] == "Артем Белов"
    task, _ = call(client, emp, "get_task", {"number": got["number"]})
    assert task["text"] == "Счета поставщиков приходят в личную почту и теряются."  # тема в тексте уже есть, подпись отрезана
    assert call(client, admin, "check_mailbox")[1]["isError"]  # ящик в тестах не настроен
    assert "admin" in call(client, admin, "get_channels")[0]


def test_mailbox_takes_only_feedback_address(client, monkeypatch):
    """IMAP_TO: из общего ящика берутся только непрочитанные письма на адрес обратной связи."""
    from app import mail

    class Box:
        searched = None

        def __init__(self, host): pass
        def login(self, user, password): pass
        def select(self, folder): pass
        def logout(self): pass

        def search(self, charset, *criteria):
            Box.searched = criteria
            return "OK", [b"1"]

        def fetch(self, num, what):
            raw = ("From: Артем Белов <sales@modus.demo>\nTo: box+otklik@example.com\nSubject: Склад\n"
                   "Content-Type: text/plain; charset=utf-8\n\nНакладные со склада приходят с опозданием на два дня.\n")
            return "OK", [(b"1", raw.encode("utf-8"))]

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", Box)
    monkeypatch.setattr(settings, "imap_to", "box+otklik@example.com")
    assert mail.check_once() == 1
    assert Box.searched == ("UNSEEN", "TO", '"box+otklik@example.com"')
    monkeypatch.setattr(settings, "imap_to", "")
    mail.check_once()
    assert Box.searched == ("UNSEEN",)


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
