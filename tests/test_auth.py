"""Auth is the control that matters most here: this app can send email as its
operator, so an unauthenticated instance is an open relay."""

import pytest

from tests.conftest import TEST_PASSWORD

PROTECTED_READS = ["/agents", "/agents/some-id/history", "/poll/some-job"]
PROTECTED_WRITES = [
    ("post", "/agents"),
    ("delete", "/agents/some-id"),
    ("post", "/agents/some-id/status"),
    ("delete", "/agents/some-id/history"),
    ("post", "/chat/some-id"),
    ("post", "/jobs/some-job/confirm"),
]


@pytest.mark.parametrize("path", PROTECTED_READS)
def test_protected_reads_reject_anonymous(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("method,path", PROTECTED_WRITES)
def test_protected_writes_reject_anonymous(client, method, path):
    response = getattr(client, method)(path, json={})
    assert response.status_code == 401


def test_index_redirects_anonymous_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_healthz_is_open(client):
    assert client.get("/healthz").status_code == 200


def test_wrong_password_rejected(client):
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.get("/agents").status_code == 401


def test_login_then_access(client):
    assert client.post("/login", data={"password": TEST_PASSWORD}).status_code == 302
    assert client.get("/agents").status_code == 200


def test_logout_revokes_access(auth_client):
    assert auth_client.get("/agents").status_code == 200
    auth_client.post("/logout")
    assert auth_client.get("/agents").status_code == 401


@pytest.mark.parametrize("path", ["/debug-key", "/test-api", "/direct"])
def test_removed_endpoints_are_gone(auth_client, path):
    """/debug-key leaked API-key metadata, /test-api burned credits, and /direct
    was an unscoped model proxy. All three were deleted, not just gated."""
    assert auth_client.get(path).status_code == 404
