"""Сквозные сценарии из датасета организаторов (раздел 4) на чистой базе, без сети и ИИ-ключей.

Запуск:  python -m pytest tests -q   (из папки backend)
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="modus-test-")
os.environ.update(AI_MODE="rules", BACKGROUND="0", TELEGRAM_BOT_TOKEN="", IMAP_HOST="")

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


class Chat:
    """Собеседник бота в тестах: вместо сети складываем исходящие сообщения в список."""

    def __init__(self, ch, chat_id):
        from app import bots
        self.bots, self.ch, self.id = bots, ch, chat_id
        if not hasattr(ch, "sent"):
            ch.sent = []
            ch.push = lambda chat_id, text, buttons=None, edit_id="": ch.sent.append((chat_id, text, buttons or []))

    @property
    def log(self):
        return [m for m in self.ch.sent if m[0] == self.id]

    @property
    def last(self):
        return self.log[-1][1]

    def write(self, text="", **kw):
        self.bots.handle(self.ch, self.bots.Incoming(chat_id=self.id, text=text, **kw))
        return self.last

    def tap(self, label):
        """Нажать кнопку из недавних сообщений по началу подписи."""
        for _, _, rows in reversed(self.log[-4:]):
            for row in rows:
                for text, data in row:
                    if text.startswith(label):
                        self.bots.handle(self.ch, self.bots.Incoming(chat_id=self.id, callback=data, message_id="1"))
                        return self.last
        raise AssertionError(f"нет кнопки «{label}»: {self.log[-1]}")

    def link(self, client, headers):
        code = client.post(f"/api/me/messengers/{self.ch.key}/code", headers=headers).json()
        return self.write("/start", start_code=code["code"]), code


def test_messenger_dialog(client):
    """Диалог с Telegram-ботом без сети: отправку подменяем."""
    from app import bots
    ch = bots.channels()["telegram"]
    ch.state.update(enabled=True, username="modus_otklik_bot")
    emp, boss = login(client, "pm"), login(client, "director")
    me = Chat(ch, "777")

    assert "для сотрудников" in me.write("Здравствуйте")
    hello, link = me.link(client, emp)
    assert link["link"] == f"https://t.me/modus_otklik_bot?start={link['code']}"
    assert "Telegram привязан" in hello
    assert next(m for m in client.get("/api/me/messengers", headers=emp).json() if m["key"] == "telegram")["linked"]

    # текст становится черновиком: до подтверждения обращение не создается
    assert "подробнее" in me.write("ой")
    before = len(client.get("/api/appeals?scope=mine", headers=emp).json())
    card = me.write("На объекте не хватает актуальных схем подключения, монтажники звонят проектировщикам по каждому вопросу.")
    assert "Черновик обращения" in card and "Рассмотрит:" in card
    assert len(client.get("/api/appeals?scope=mine", headers=emp).json()) == before
    assert "Анонимно: ваше имя" in me.tap("Анонимно: нет")
    assert "Анонимно: ваше имя" not in me.tap("Анонимно: да")
    me.tap("Категория")
    assert "Каталог и данные" in me.tap("Каталог и данные")
    assert "(дополнение)" in me.write("(дополнение) Особенно на складе.")       # следующее сообщение дополняет черновик
    assert "Каталог и данные" in me.last                                         # выбранное вручную не перезаписывается
    assert "Зарегистрировано: A-" in me.tap("Отправить")
    a = next(r for r in client.get("/api/appeals?scope=mine", headers=emp).json() if r["source"] == "telegram")
    assert a["category"] == "Каталог и данные" and a["assignee"]

    # уточнение приходит в мессенджер, следующий текст - ответ
    client.post(f"/api/appeals/{a['id']}/clarify", json={"text": "О каком объекте речь?"}, headers=boss)
    assert "О каком объекте" in me.last and me.log[-1][2][0][0][0] == "Ответить"
    me.write("Склад в Павлодаре")
    assert any("передан ответственному" in m[1] for m in me.log[-2:])
    d = client.get(f"/api/appeals/{a['id']}", headers=boss).json()
    assert d["status"] == "review" and any(e["kind"] == "answer" and "Павлодар" in e["text"] for e in d["events"])

    assert a["number"] in me.write("/my")
    assert a["title"] in me.tap(a["number"])                                     # карточка обращения
    me.tap("Комментарий")
    assert "Комментарий добавлен" in me.write("Схемы нужны к понедельнику")

    # результат: автор не согласен - обращение возвращается в работу
    client.post(f"/api/appeals/{a['id']}/status", json={"status": "in_progress"}, headers=boss)
    client.post(f"/api/appeals/{a['id']}/status", json={"status": "done", "text": "Схемы выложены в общий каталог."}, headers=boss)
    assert "Реализовано" in me.last
    assert "Что осталось" in me.tap("Не решено")
    assert "Вернул" in me.write("В каталоге старые версии схем")
    d = client.get(f"/api/appeals/{a['id']}", headers=boss).json()
    assert d["status"] == "in_progress" and d["rating"] == -1 and any(e["kind"] == "rated" for e in d["events"])
    client.post(f"/api/appeals/{a['id']}/status", json={"status": "done", "text": "Обновили схемы."}, headers=boss)
    assert "Спасибо" in me.tap("Решено")
    assert client.get(f"/api/appeals/{a['id']}", headers=emp).json()["rating"] == 1

    # похожее обращение: присоединиться вместо дубля
    other = Chat(ch, "778")
    other.link(client, login(client, "design"))
    card = other.write("Обсуждения по задаче идут в разных каналах и мессенджерах, контекст теряется, нужно единое место для решений.")
    assert "A-012" in card
    assert "Присоединил вас к A-012" in other.tap("У меня то же: A-012")
    assert "уже отправлен или отменен" in other.tap("Отправить")                 # черновик закрыт, дубль не создать
    client.post("/api/me/messengers/telegram/unlink", headers=login(client, "design"))

    # фото без подписи -> описание -> обращение с вложением
    bots._download = lambda ch, inc: {"filename": inc.file_name, "stored": "test-photo.jpg", "size": 5}
    assert "Файл получил" in me.write(file_url="x", file_name="щит.jpg")
    assert "Вложений: 1" in me.write("В щитовой на втором этаже нет маркировки автоматов, фото прилагаю.")
    me.tap("Отправить")
    b = next(r for r in client.get("/api/appeals?scope=mine", headers=emp).json() if r["source"] == "telegram")
    assert [f["filename"] for f in client.get(f"/api/appeals/{b['id']}", headers=emp).json()["attachments"]] == ["щит.jpg"]

    client.post("/api/me/messengers/telegram/unlink", headers=emp)
    ch.state.update(enabled=False)


def test_handler_works_from_bot(client):
    """Ответственный разбирает входящие кнопками: взять в работу, уточнить, закрыть с итогом."""
    from app import bots
    ch = bots.channels()["telegram"]
    ch.state.update(enabled=True, username="modus_otklik_bot")
    emp, mgr = login(client, "sales"), login(client, "contracts")
    boss = Chat(ch, "888")
    boss.link(client, mgr)
    cats = {c["name"]: c["id"] for c in client.get("/api/meta", headers=emp).json()["categories"]}
    a = client.post("/api/appeals", data={"text": "Шаблон договора поставки не содержит пункта о сроках приемки оборудования.",
                                          "type": "problem", "category_id": cats["Договорная работа"], "priority": "medium",
                                          "title": "Нет сроков приемки в шаблоне"}, headers=emp).json()
    assert f"Новое обращение {a['number']}" in boss.last
    assert a["number"] in boss.write("/inbox")
    assert "В работе" in boss.tap("Взять в работу")
    boss.tap("Уточнить")
    assert "Вопрос отправлен автору" in boss.write("Для каких поставщиков?")
    assert client.get(f"/api/appeals/{a['id']}", headers=emp).json()["status"] == "clarify"
    boss.tap("Открыть")
    assert "На рассмотрении" in boss.tap("Вернуть на рассмотрение")
    boss.tap("Взять в работу")
    assert "опишите, что сделано" in boss.tap("Готово")
    assert "Реализовано" in boss.write("Пункт о сроках приемки добавлен в шаблон.")
    d = client.get(f"/api/appeals/{a['id']}", headers=emp).json()
    assert d["status"] == "done" and d["resolution"].startswith("Пункт") and d["can"]["rate"]
    # посторонний сотрудник чужую карточку кнопкой не откроет
    client.post("/api/me/messengers/telegram/unlink", headers=mgr)
    stranger = Chat(ch, "999")
    stranger.link(client, login(client, "office"))
    bots.handle(ch, bots.Incoming(chat_id="999", callback=f"st:{a['id']}:in_progress"))
    assert "не найдено" in stranger.last
    client.post("/api/me/messengers/telegram/unlink", headers=login(client, "office"))
    ch.state.update(enabled=False)


def test_telegram_mini_app_login(client):
    """Mini App: вход по подписанным данным запуска Telegram, привязка без кода."""
    import hashlib, hmac, json, time
    from urllib.parse import urlencode
    from app.config import settings

    def init_data(tg_id, token="1234:TEST", age=0):
        pairs = {"auth_date": str(int(time.time()) - age), "query_id": "q1", "user": json.dumps({"id": tg_id, "first_name": "T"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        return urlencode({**pairs, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})

    assert client.post("/api/auth/telegram", json={"init_data": init_data(555)}).status_code == 404  # бот не подключен
    settings.telegram_token = "1234:TEST"
    try:
        assert client.post("/api/auth/telegram", json={"init_data": init_data(555)}).status_code == 404  # еще не привязан
        assert client.post("/api/auth/telegram", json={"init_data": init_data(555, token="9:FAKE")}).status_code == 401
        assert client.post("/api/auth/telegram", json={"init_data": init_data(555, age=3 * 24 * 3600)}).status_code == 401
        assert client.post("/api/auth/telegram", json={"init_data": init_data(555).replace("555", "556")}).status_code == 401
        emp = login(client, "design")
        assert client.post("/api/me/messengers/telegram/webapp", json={"init_data": init_data(555)}, headers=emp).json()["telegram"]
        r = client.post("/api/auth/telegram", json={"init_data": init_data(555)})
        assert r.status_code == 200 and r.json()["user"]["email"] == "design@modus.demo"
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        assert client.get("/api/me", headers=h).json()["email"] == "design@modus.demo"
        # тот же Telegram привязывает другой сотрудник - у прежнего привязка снимается
        client.post("/api/me/messengers/telegram/webapp", json={"init_data": init_data(555)}, headers=login(client, "office"))
        assert client.post("/api/auth/telegram", json={"init_data": init_data(555)}).json()["user"]["email"] == "office@modus.demo"
        client.post("/api/me/messengers/telegram/unlink", headers=login(client, "office"))
    finally:
        settings.telegram_token = ""


def test_email_intake(client):
    adm = login(client, "admin")
    r = client.post("/api/admin/intake/email", json={"sender": "Жанна <acc@modus.demo>", "subject": "Re: Акты сверки",
        "body": "Добрый день. Акты сверки приходят в разных форматах, приходится сводить вручную.\n\nС уважением, Жанна\n> старая переписка"}, headers=adm)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["source"] == "email" and a["author"]["name"] == "Жанна Мухамедова"
    full = client.get(f"/api/appeals/{a['id']}", headers=adm).json()
    assert "старая переписка" not in full["text"] and "С уважением" not in full["text"]
