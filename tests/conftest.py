"""Test fixtures.

config.py reads the environment at import time, so every setting has to be in
place before app/config/db are imported. Hence the env writes at module top.
"""

import os
import tempfile
import threading

import pytest

# Captured before any fixture patches it. run_threads_inline swaps
# threading.Thread on the real module (app.py does `import threading`, so
# app_module.threading *is* that module), which would otherwise force genuinely
# concurrent tests to run inline and deadlock.
REAL_THREAD = threading.Thread

_TMPDIR = tempfile.mkdtemp(prefix="cbm-tests-")

os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
os.environ["APP_PASSWORD"] = "test-password-123"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
os.environ["GMAIL_USER"] = "bot@example.com"
os.environ["GMAIL_APP_PASSWORD"] = "test-gmail-password"
os.environ.pop("ALLOWED_ORIGINS", None)
os.environ.pop("GMAIL_ALLOWED_RECIPIENTS", None)

import app as app_module  # noqa: E402
import db  # noqa: E402

TEST_PASSWORD = "test-password-123"


@pytest.fixture(autouse=True)
def clean_database():
    """Every test starts from an empty database."""
    engine = db.get_engine()
    db.metadata.drop_all(engine)
    db.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def auth_client(client):
    response = client.post("/login", data={"password": TEST_PASSWORD})
    assert response.status_code == 302
    return client


@pytest.fixture
def run_threads_inline(monkeypatch):
    """Run background jobs synchronously so tests can assert on the outcome.

    The chat routes hand work to a thread; without this the assertions would
    race the worker.
    """

    class InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(app_module.threading, "Thread", InlineThread)


@pytest.fixture
def sent_emails(monkeypatch):
    """Record SMTP sends instead of performing them."""
    recorded = []
    monkeypatch.setattr(
        app_module, "send_gmail", lambda to, subject, body: recorded.append((to, subject, body))
    )
    return recorded
