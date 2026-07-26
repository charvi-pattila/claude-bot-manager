"""End-to-end chat flow, with the model and SMTP stubbed out.

The assertion that matters: nothing reaches SMTP without an explicit confirm.
"""

import pytest

import app as app_module
import config
import db
import llm
from tests.test_llm import FakeClient, FakeResponse, text, tool_use

EMAIL = {"to": "friend@example.com", "subject": "Trip", "body": "Here is the plan."}


@pytest.fixture
def sent_emails(monkeypatch):
    """Record SMTP sends instead of performing them."""
    recorded = []
    monkeypatch.setattr(
        app_module, "send_gmail", lambda to, subject, body: recorded.append((to, subject, body))
    )
    return recorded


def make_agent(auth_client, name="Mailer"):
    return auth_client.post("/agents", json={"name": name}).get_json()["id"]


def stub_model(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return client


def test_plain_chat_round_trip(auth_client, monkeypatch, run_threads_inline):
    agent_id = make_agent(auth_client)
    stub_model(monkeypatch, [FakeResponse([text("Hi!")], "end_turn")])

    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "hello"}).get_json()["job_id"]
    job = auth_client.get(f"/poll/{job_id}").get_json()

    assert job["status"] == "done"
    assert job["reply"] == "Hi!"
    # Both sides of the exchange are persisted.
    assert [m["content"] for m in db.list_messages(agent_id)] == ["hello", "Hi!"]


def test_email_is_not_sent_without_confirmation(
    auth_client, monkeypatch, run_threads_inline, sent_emails
):
    agent_id = make_agent(auth_client)
    stub_model(monkeypatch, [FakeResponse([tool_use("t1", "send_email", EMAIL)], "tool_use")])

    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "email my friend"}).get_json()[
        "job_id"
    ]
    job = auth_client.get(f"/poll/{job_id}").get_json()

    assert job["status"] == "awaiting_confirmation"
    assert job["pending_email"] == EMAIL
    assert sent_emails == []


def test_confirm_sends(auth_client, monkeypatch, run_threads_inline, sent_emails):
    agent_id = make_agent(auth_client)
    stub_model(
        monkeypatch,
        [
            FakeResponse([tool_use("t1", "send_email", EMAIL)], "tool_use"),
            FakeResponse([text("Sent it.")], "end_turn"),
        ],
    )
    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "email"}).get_json()["job_id"]

    auth_client.post(f"/jobs/{job_id}/confirm", json={"approve": True})

    assert sent_emails == [(EMAIL["to"], EMAIL["subject"], EMAIL["body"])]
    assert auth_client.get(f"/poll/{job_id}").get_json()["status"] == "done"


def test_decline_does_not_send(auth_client, monkeypatch, run_threads_inline, sent_emails):
    agent_id = make_agent(auth_client)
    stub_model(
        monkeypatch,
        [
            FakeResponse([tool_use("t1", "send_email", EMAIL)], "tool_use"),
            FakeResponse([text("Okay, I won't.")], "end_turn"),
        ],
    )
    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "email"}).get_json()["job_id"]

    auth_client.post(f"/jobs/{job_id}/confirm", json={"approve": False})

    assert sent_emails == []
    assert auth_client.get(f"/poll/{job_id}").get_json()["reply"] == "Okay, I won't."


def test_confirm_rejected_when_not_awaiting(auth_client, monkeypatch, run_threads_inline):
    agent_id = make_agent(auth_client)
    stub_model(monkeypatch, [FakeResponse([text("Hi!")], "end_turn")])
    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "hi"}).get_json()["job_id"]

    assert auth_client.post(f"/jobs/{job_id}/confirm", json={"approve": True}).status_code == 409


def test_missing_api_key_returns_503(auth_client, monkeypatch):
    agent_id = make_agent(auth_client)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    response = auth_client.post(f"/chat/{agent_id}", json={"message": "hi"})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.get_json()["error"]


def test_chat_to_unknown_agent_is_404(auth_client):
    assert auth_client.post("/chat/nope", json={"message": "hi"}).status_code == 404


def test_empty_message_rejected(auth_client):
    agent_id = make_agent(auth_client)
    assert auth_client.post(f"/chat/{agent_id}", json={"message": "  "}).status_code == 400


def test_model_error_surfaces_to_poll(auth_client, monkeypatch, run_threads_inline):
    agent_id = make_agent(auth_client)

    def boom():
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(llm, "get_client", boom)
    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "hi"}).get_json()["job_id"]
    job = auth_client.get(f"/poll/{job_id}").get_json()

    assert job["status"] == "error"
    assert "upstream exploded" in job["error"]
    # The agent must not be stranded in "running".
    assert db.get_agent(agent_id)["status"] == "idle"


def test_recipient_allowlist_blocks_unknown_address(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_ALLOWED_RECIPIENTS", {"ok@example.com"})
    with pytest.raises(ValueError, match="GMAIL_ALLOWED_RECIPIENTS"):
        app_module.send_gmail("stranger@example.com", "s", "b")
