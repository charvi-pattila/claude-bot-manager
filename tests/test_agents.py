import config
import db


def test_create_and_list(auth_client):
    response = auth_client.post("/agents", json={"name": "Travel", "type": "bot"})
    assert response.status_code == 201
    created = response.get_json()
    assert created["name"] == "Travel"
    assert created["status"] == "idle"

    listed = auth_client.get("/agents").get_json()
    assert [a["id"] for a in listed] == [created["id"]]


def test_create_requires_name(auth_client):
    assert auth_client.post("/agents", json={"name": "   "}).status_code == 400


def test_delete_removes_agent(auth_client):
    agent_id = auth_client.post("/agents", json={"name": "Temp"}).get_json()["id"]
    assert auth_client.delete(f"/agents/{agent_id}").status_code == 200
    assert auth_client.get("/agents").get_json() == []


def test_delete_missing_agent_is_404(auth_client):
    assert auth_client.delete("/agents/does-not-exist").status_code == 404


def test_ids_do_not_collide_after_delete(auth_client):
    """Regression: IDs used to be str(len(agents) + 1).

    Creating two agents, deleting the first, then creating a third handed the
    new agent an ID that already belonged to an existing one — and with it, that
    agent's chat history.
    """
    first = auth_client.post("/agents", json={"name": "First"}).get_json()["id"]
    second = auth_client.post("/agents", json={"name": "Second"}).get_json()["id"]
    auth_client.delete(f"/agents/{first}")
    third = auth_client.post("/agents", json={"name": "Third"}).get_json()["id"]

    assert third != second
    assert len({a["id"] for a in auth_client.get("/agents").get_json()}) == 2


def test_delete_cascades_history(auth_client):
    agent_id = auth_client.post("/agents", json={"name": "Chatty"}).get_json()["id"]
    db.add_message(agent_id, "user", "hello")
    auth_client.delete(f"/agents/{agent_id}")
    assert db.list_messages(agent_id) == []


def test_status_rejects_unknown_value(auth_client):
    agent_id = auth_client.post("/agents", json={"name": "S"}).get_json()["id"]
    bad = auth_client.post(f"/agents/{agent_id}/status", json={"status": "on fire"})
    good = auth_client.post(f"/agents/{agent_id}/status", json={"status": "running"})
    assert bad.status_code == 400
    assert good.status_code == 200


def test_history_is_scoped_per_agent(auth_client):
    a = auth_client.post("/agents", json={"name": "A"}).get_json()["id"]
    b = auth_client.post("/agents", json={"name": "B"}).get_json()["id"]
    db.add_message(a, "user", "for A")
    db.add_message(b, "user", "for B")

    assert [m["content"] for m in auth_client.get(f"/agents/{a}/history").get_json()] == ["for A"]
    assert [m["content"] for m in auth_client.get(f"/agents/{b}/history").get_json()] == ["for B"]


def test_history_limit_returns_most_recent(auth_client):
    agent_id = auth_client.post("/agents", json={"name": "Long"}).get_json()["id"]
    for i in range(config.HISTORY_LIMIT + 10):
        db.add_message(agent_id, "user", f"message {i}")

    capped = db.list_messages(agent_id, limit=config.HISTORY_LIMIT)
    assert len(capped) == config.HISTORY_LIMIT
    # Newest kept, and still in chronological order.
    assert capped[-1]["content"] == f"message {config.HISTORY_LIMIT + 9}"
    assert capped[0]["content"] == "message 10"


def test_agents_survive_a_new_engine(auth_client):
    """Agents live in the database, not process memory.

    The previous build kept them in a JSON file on the container filesystem,
    which meant every redeploy silently wiped them.
    """
    agent_id = auth_client.post("/agents", json={"name": "Durable"}).get_json()["id"]
    db._engine = None  # force a fresh connection, as a restart would
    assert any(a["id"] == agent_id for a in db.list_agents())
