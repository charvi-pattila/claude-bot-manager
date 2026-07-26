"""Regressions for every blocking/major bug found in independent QA review.

Each test here corresponds to a specific defect that shipped in the first cut of
this branch and was caught only by review, not by the original suite.
"""

import threading

import pytest

import app as app_module
import config
import db
from tests.conftest import REAL_THREAD
from tests.test_chat_flow import EMAIL, make_agent, stub_model
from tests.test_llm import FakeResponse, text, tool_use

# --- Blocking: empty assistant reply bricked the agent ----------------------


def test_empty_reply_is_not_persisted(auth_client, monkeypatch, run_threads_inline):
    """A turn whose text is empty (thinking ate max_tokens) must not be stored.

    Persisting "" poisoned every later turn — the API rejects empty content and
    there is no in-app way to clear history, so the agent was dead for good.
    """
    agent_id = make_agent(auth_client)
    stub_model(monkeypatch, [FakeResponse([text("   ")], "end_turn")])

    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "hi"}).get_json()["job_id"]
    job = auth_client.get(f"/poll/{job_id}").get_json()

    assert job["status"] == "error"
    assert "MAX_TOKENS" in job["error"]
    assert [m["role"] for m in db.list_messages(agent_id)] == ["user"]
    assert db.get_agent(agent_id)["status"] == "idle"


# --- Blocking: history window could start with an assistant message ---------


@pytest.mark.parametrize("turns", [20, 25, 30, 41])
@pytest.mark.parametrize("trailing_user", [False, True])
def test_history_window_always_starts_with_user(auth_client, turns, trailing_user):
    """The API rejects a conversation starting with an assistant message.

    Slicing "newest N" of an alternating history landed on an assistant message
    for half of all history lengths, permanently breaking agents past the limit.
    """
    agent_id = make_agent(auth_client)
    for i in range(turns):
        db.add_message(agent_id, "user", f"u{i}")
        db.add_message(agent_id, "assistant", f"a{i}")

    if trailing_user:
        # Odd total length is the shape that actually shifted the window onto an
        # assistant message; without it this case passes either way.
        db.add_message(agent_id, "user", "trailing")

    window = db.list_messages(agent_id, limit=config.HISTORY_LIMIT)
    assert window, "window should not be empty"
    assert window[0]["role"] == "user"
    assert len(window) <= config.HISTORY_LIMIT


def test_history_window_odd_length_starts_with_user(auth_client):
    agent_id = make_agent(auth_client)
    for i in range(config.HISTORY_LIMIT + 7):
        db.add_message(agent_id, "user", f"u{i}")
        db.add_message(agent_id, "assistant", f"a{i}")
    db.add_message(agent_id, "user", "trailing")

    assert db.list_messages(agent_id, limit=config.HISTORY_LIMIT)[0]["role"] == "user"


# --- Blocking: legacy import re-ran and could crash the boot ----------------


def test_legacy_import_does_not_resurrect_deleted_agents(tmp_path, monkeypatch):
    """Guarded on a migration marker, not on "the agents table is empty"."""
    legacy = tmp_path / "bots.json"
    legacy.write_text('[{"name": "Travel", "type": "bot", "instructions": "x"}]')

    db._import_legacy_bots_json(str(legacy))
    imported = db.list_agents()
    assert [a["name"] for a in imported] == ["Travel"]

    for agent in imported:
        db.delete_agent(agent["id"])

    db._import_legacy_bots_json(str(legacy))  # a restart
    assert db.list_agents() == []


def test_legacy_import_survives_malformed_entries(tmp_path):
    """init_db() runs at import time, so a bad row used to crash-loop workers."""
    legacy = tmp_path / "bots.json"
    legacy.write_text('[{"name": 12345}, {"name": null}, "not-an-object", {"name": "Good"}]')

    db._import_legacy_bots_json(str(legacy))  # must not raise

    assert [a["name"] for a in db.list_agents()] == ["Good"]


def test_legacy_import_survives_unparseable_file(tmp_path):
    legacy = tmp_path / "bots.json"
    legacy.write_text("{not json at all")
    db._import_legacy_bots_json(str(legacy))
    assert db.list_agents() == []


# --- Blocking: the email gate was bypassable three ways ---------------------


@pytest.fixture
def parked_job(auth_client, monkeypatch, run_threads_inline):
    """A job parked awaiting confirmation of EMAIL."""
    agent_id = make_agent(auth_client)
    stub_model(
        monkeypatch,
        [
            FakeResponse([tool_use("t1", "send_email", EMAIL)], "tool_use"),
            FakeResponse([text("Done.")], "end_turn"),
        ],
    )
    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "email"}).get_json()["job_id"]
    assert auth_client.get(f"/poll/{job_id}").get_json()["status"] == "awaiting_confirmation"
    return job_id


@pytest.mark.parametrize("value", ["false", "no", "0", "off", "true", 1, 0, [], {}, None])
def test_approve_must_be_a_real_boolean(auth_client, parked_job, sent_emails, value):
    """bool("false") is True — the gate used to approve on a decline payload."""
    token = auth_client.get(f"/poll/{parked_job}").get_json()["pending_token"]
    response = auth_client.post(
        f"/jobs/{parked_job}/confirm", json={"approve": value, "pending_token": token}
    )
    assert response.status_code == 400
    assert sent_emails == []


def test_confirm_requires_the_token(auth_client, parked_job, sent_emails):
    assert (
        auth_client.post(f"/jobs/{parked_job}/confirm", json={"approve": True}).status_code == 400
    )
    bad = auth_client.post(
        f"/jobs/{parked_job}/confirm", json={"approve": True, "pending_token": "wrong"}
    )
    assert bad.status_code == 409
    assert sent_emails == []


def test_replayed_confirm_cannot_send_twice(
    auth_client, parked_job, sent_emails, run_threads_inline
):
    """The token is single-use, so a replayed POST cannot approve a later,
    unseen message that parked in the meantime."""
    token = auth_client.get(f"/poll/{parked_job}").get_json()["pending_token"]
    body = {"approve": True, "pending_token": token}

    first = auth_client.post(f"/jobs/{parked_job}/confirm", json=body)
    replay = auth_client.post(f"/jobs/{parked_job}/confirm", json=body)

    assert first.status_code == 200
    assert replay.status_code == 409
    assert len(sent_emails) == 1


def test_stale_confirm_cannot_approve_a_re_parked_message(
    auth_client, monkeypatch, run_threads_inline, sent_emails
):
    """The attack the token exists for.

    A confirm approves one specific message. If the resumed turn parks AGAIN
    with a different email, replaying the first confirm must not approve the
    second — the user never saw it. The job status is back to
    awaiting_confirmation at that point, so the status check alone does not
    help; only the token does.
    """
    evil = {"to": "attacker@evil.com", "subject": "Wire transfer", "body": "send money"}
    agent_id = make_agent(auth_client)
    stub_model(
        monkeypatch,
        [
            FakeResponse([tool_use("t1", "send_email", EMAIL)], "tool_use"),
            FakeResponse([tool_use("t2", "send_email", evil)], "tool_use"),
        ],
    )
    job_id = auth_client.post(f"/chat/{agent_id}", json={"message": "email"}).get_json()["job_id"]

    first_token = auth_client.get(f"/poll/{job_id}").get_json()["pending_token"]
    body = {"approve": True, "pending_token": first_token}
    assert auth_client.post(f"/jobs/{job_id}/confirm", json=body).status_code == 200

    # The resumed turn parked again, on a message the user has not seen.
    parked_again = auth_client.get(f"/poll/{job_id}").get_json()
    assert parked_again["status"] == "awaiting_confirmation"
    assert parked_again["pending_email"] == evil
    assert parked_again["pending_token"] != first_token

    replay = auth_client.post(f"/jobs/{job_id}/confirm", json=body)

    assert replay.status_code == 409
    assert [e[0] for e in sent_emails] == [EMAIL["to"]], "the unseen email must not be sent"


def test_concurrent_confirms_send_once(auth_client, parked_job, sent_emails, monkeypatch):
    """Two simultaneous confirms both used to pass the status check and send.

    The claim is a conditional UPDATE, so exactly one wins regardless of timing
    or which worker process handles each request.
    """
    token = auth_client.get(f"/poll/{parked_job}").get_json()["pending_token"]
    claimed = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)  # line both threads up before the claim
        claimed.append(db.claim_confirmation(parked_job, token))

    threads = [REAL_THREAD(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert claimed.count(True) == 1, f"expected exactly one winner, got {claimed}"
    assert claimed.count(False) == 1


# --- Major: resume must validate state before sending -----------------------


def test_unusable_resume_state_does_not_send(auth_client, parked_job, sent_emails):
    """The send is irreversible, so it must come after the state is known good.

    Otherwise the mail goes out, the follow-up turn fails, and the user is shown
    only an error — with nothing recording that the message was delivered.
    """
    db.update_job(parked_job, conversation=None)
    token = db.get_job(parked_job)["pending_token"]

    response = auth_client.post(
        f"/jobs/{parked_job}/confirm", json={"approve": True, "pending_token": token}
    )
    assert response.status_code == 200

    job = auth_client.get(f"/poll/{parked_job}").get_json()
    assert job["status"] == "error"
    assert "No email was sent" in job["error"]
    assert sent_emails == []


def test_deleted_agent_midflight_does_not_hang_the_job(auth_client, parked_job, sent_emails):
    """The job row lookup used to sit outside the try, so the worker thread died
    uncaught and the frontend polled a 'pending' job forever."""
    job = db.get_job(parked_job)
    db.delete_agent(job["agent_id"])

    response = auth_client.post(
        f"/jobs/{parked_job}/confirm",
        json={"approve": True, "pending_token": job["pending_token"]},
    )
    assert response.status_code == 404
    assert sent_emails == []


# --- Major: recipient must be exactly one address ---------------------------


@pytest.mark.parametrize(
    "bad", ["a@example.com,b@evil.com", "", "not-an-address", "a@b@c.com", "a@example.com, b@x.com"]
)
def test_send_gmail_rejects_non_single_recipients(monkeypatch, bad):
    monkeypatch.setattr(config, "GMAIL_ALLOWED_RECIPIENTS", set())
    with pytest.raises(ValueError):
        app_module.send_gmail(bad, "subject", "body")


# --- Major: login throttling ------------------------------------------------


def test_login_is_throttled(client, monkeypatch):
    """compare_digest closes the timing channel but not the guessing channel."""
    monkeypatch.setattr(config, "LOGIN_MAX_ATTEMPTS", 3)
    app_module._login_failures.clear()

    codes = [client.post("/login", data={"password": "wrong"}).status_code for _ in range(5)]

    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]


def test_non_ascii_password_does_not_500(client, monkeypatch):
    """compare_digest raises TypeError on non-ASCII str; must compare bytes."""
    monkeypatch.setattr(config, "APP_PASSWORD", "pässwörd-123")
    app_module._login_failures.clear()
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    assert client.post("/login", data={"password": "pässwörd-123"}).status_code == 302
