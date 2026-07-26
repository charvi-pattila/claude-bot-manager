"""Flask app: routes, auth, and the chat job lifecycle.

Storage lives in db.py, model calls in llm.py, settings in config.py.

Two behaviours are load-bearing and easy to undo by accident:

* Every route except ``/login`` and ``/healthz`` requires a session. This app can
  send email as its operator, so an open instance is an open relay.
* A ``send_email`` tool call parks the turn in ``awaiting_confirmation``. Nothing
  is delivered until a human looks at the actual recipient and body and clicks
  confirm.
"""

from __future__ import annotations

import hmac
import smtplib
import threading
import time
import uuid
from collections import defaultdict
from datetime import timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from functools import wraps
from threading import Lock
from typing import Any

from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_cors import CORS

import config
import db
import llm

config.validate()

app = Flask(__name__)
# Required, not optional: with more than one gunicorn worker a per-process
# random key means each worker signs cookies differently, so a login bounces at
# random depending on which worker answers. config.validate() enforces it.
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Tied to an explicit flag rather than inferred from an unrelated CORS
    # setting, which previously left HTTPS deploys serving a non-Secure cookie.
    SESSION_COOKIE_SECURE=config.SECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_DAYS),
)

# The old build used a bare CORS(app), which let any website on the internet
# call this API from a logged-in user's browser.
if config.ALLOWED_ORIGINS:
    CORS(app, origins=sorted(config.ALLOWED_ORIGINS), supports_credentials=True)

db.init_db()


# --- Auth ------------------------------------------------------------------


def login_required(view):
    """Guard a JSON endpoint. Answers 401 so the frontend can react.

    Deliberately *not* a redirect: fetch() follows redirects transparently, so
    a 302 to the HTML login page would arrive as unparseable HTML and the UI
    would fail with a JSON error instead of sending the user to log in.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"error": "Authentication required"}), 401
        return view(*args, **kwargs)

    return wrapped


def page_login_required(view):
    """Guard an HTML page. Redirects, because a human is looking at it."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


# Failed-login timestamps per client address. A single shared password guarding
# an endpoint that sends mail deserves more than a constant-time compare:
# compare_digest closes the timing channel but does nothing about guessing.
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_lock = Lock()


def _throttle_key() -> str:
    return request.remote_addr or "unknown"


def _login_blocked() -> bool:
    now = time.monotonic()
    with _login_lock:
        recent = [t for t in _login_failures[_throttle_key()] if now - t < config.LOGIN_WINDOW_S]
        _login_failures[_throttle_key()] = recent
        return len(recent) >= config.LOGIN_MAX_ATTEMPTS


def _record_login_failure() -> None:
    with _login_lock:
        _login_failures[_throttle_key()].append(time.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if _login_blocked():
            return render_template(
                "login.html", error="Too many attempts. Wait a minute and try again."
            ), 429

        supplied = (request.form.get("password") or "").strip()
        # Compare bytes: compare_digest raises TypeError on non-ASCII str, which
        # would 500 rather than reject a wrong password.
        if hmac.compare_digest(supplied.encode(), config.APP_PASSWORD.encode()):
            session.clear()
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))

        _record_login_failure()
        return render_template("login.html", error="Incorrect password."), 401
    if session.get("authed"):
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe. Deliberately reveals nothing."""
    return jsonify({"status": "ok"})


# --- Pages -----------------------------------------------------------------


@app.route("/")
@page_login_required
def index():
    response = make_response(render_template("index.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


# --- Agents ----------------------------------------------------------------


@app.route("/agents", methods=["GET"])
@login_required
def get_agents():
    return jsonify(db.list_agents())


@app.route("/agents", methods=["POST"])
@login_required
def create_agent():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    agent = db.create_agent(
        name=name,
        agent_type=(data.get("type") or "bot").strip(),
        instructions=(data.get("instructions") or "").strip(),
    )
    return jsonify(agent), 201


@app.route("/agents/<agent_id>", methods=["DELETE"])
@login_required
def delete_agent(agent_id: str):
    if not db.get_agent(agent_id):
        return jsonify({"error": "Agent not found"}), 404
    db.delete_agent(agent_id)
    return jsonify({"message": "Deleted"})


@app.route("/agents/<agent_id>/status", methods=["POST"])
@login_required
def update_status(agent_id: str):
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in {"idle", "running"}:
        return jsonify({"error": "status must be 'idle' or 'running'"}), 400
    if not db.get_agent(agent_id):
        return jsonify({"error": "Agent not found"}), 404
    db.set_agent_status(agent_id, status)
    return jsonify({"message": "Updated"})


@app.route("/agents/<agent_id>/history", methods=["GET"])
@login_required
def get_history(agent_id: str):
    return jsonify(db.list_messages(agent_id))


@app.route("/agents/<agent_id>/history", methods=["DELETE"])
@login_required
def clear_history(agent_id: str):
    db.clear_messages(agent_id)
    return jsonify({"message": "Cleared"})


# --- Email -----------------------------------------------------------------


def send_gmail(to: str, subject: str, body: str) -> None:
    if not config.GMAIL_USER or not config.GMAIL_APP_PASSWORD:
        raise ValueError("Gmail credentials are not configured on the server.")

    # Exactly one recipient. A comma-joined string would otherwise reach
    # sendmail as a single RCPT TO and fan the message out past the address the
    # user actually approved.
    name, addr = parseaddr(to or "")
    if not addr or "," in (to or "") or addr.count("@") != 1:
        raise ValueError(f"Invalid recipient address: {to!r}")
    to = addr
    # Optional second gate: even a confirmed send can be restricted to known
    # addresses, so a misclick cannot mail a stranger.
    if config.GMAIL_ALLOWED_RECIPIENTS and to not in config.GMAIL_ALLOWED_RECIPIENTS:
        raise ValueError(f"{to} is not in GMAIL_ALLOWED_RECIPIENTS.")

    message = MIMEMultipart()
    message["From"] = config.GMAIL_USER
    message["To"] = to
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        server.sendmail(config.GMAIL_USER, to, message.as_string())


# --- Chat ------------------------------------------------------------------


def _finish(job_id: str, agent_id: str, outcome: dict[str, Any]) -> None:
    """Persist the end state of a turn."""
    if outcome["type"] == "needs_confirmation":
        # Fresh token per parked message. The client must echo it back, so an
        # approval can only ever apply to the message it was issued for.
        db.update_job(
            job_id,
            status="awaiting_confirmation",
            pending_email=outcome["email"],
            pending_token=str(uuid.uuid4()),
            conversation={
                "messages": outcome["conversation"],
                "pending_tool_uses": outcome["pending_tool_uses"],
                "tool_use_id": outcome["tool_use_id"],
            },
        )
        return

    reply = (outcome["text"] or "").strip()
    if not reply:
        # Never persist an empty assistant message. The API rejects empty
        # content, so storing one poisons every later turn for this agent — and
        # there is no in-app way to clear history, so the agent would be stuck
        # for good. Usually means max_tokens was consumed by thinking.
        db.update_job(
            job_id,
            status="error",
            error=(
                "The model returned no text. This usually means MAX_TOKENS was used up "
                "before it finished — try raising MAX_TOKENS or lowering EFFORT."
            ),
        )
        db.set_agent_status(agent_id, "idle")
        return

    db.add_message(agent_id, "assistant", reply)
    db.update_job(job_id, status="done", reply=reply)
    db.set_agent_status(agent_id, "idle")


def _run_turn_async(job_id: str, agent_id: str, system: str, conversation: list) -> None:
    try:
        client = llm.get_client()
        outcome = llm.run_turn(client, system, conversation)
        _finish(job_id, agent_id, outcome)
    except Exception as exc:  # surfaced to the user via /poll
        db.update_job(job_id, status="error", error=str(exc))
        db.set_agent_status(agent_id, "idle")


@app.route("/chat/<agent_id>", methods=["POST"])
@login_required
def chat(agent_id: str):
    agent = db.get_agent(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    if not config.ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on the server."}), 503

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    db.add_message(agent_id, "user", message)
    # Only the most recent turns are re-sent: the API is stateless, so an
    # uncapped history is re-billed in full on every single message.
    history = db.list_messages(agent_id, limit=config.HISTORY_LIMIT)
    conversation = [{"role": m["role"], "content": m["content"]} for m in history]

    db.set_agent_status(agent_id, "running")
    job_id = db.create_job(agent_id)

    threading.Thread(
        target=_run_turn_async,
        args=(job_id, agent_id, agent["instructions"], conversation),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/poll/<job_id>")
@login_required
def poll(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(
        {
            "status": job["status"],
            "reply": job["reply"],
            "error": job["error"],
            "pending_email": job["pending_email"],
            # The client echoes this back on confirm; it scopes the approval to
            # exactly the message shown above.
            "pending_token": job["pending_token"],
        }
    )


def _resume_async(
    job_id: str, agent_id: str, system: str, approve: bool, email: dict | None
) -> None:
    try:
        job = db.get_job(job_id)
        if not job:
            # Agent deleted mid-flight. Nothing left to update.
            return
        state = job["conversation"] or {}

        # Validate the resume state *before* doing anything irreversible. If the
        # persisted conversation is unusable there is no point sending: the turn
        # cannot continue afterwards, and the user would be shown a failure with
        # no hint that mail had already gone out.
        if not isinstance(state.get("messages"), list) or not state.get("pending_tool_uses"):
            db.update_job(
                job_id,
                status="error",
                error="This request expired and could not be resumed. No email was sent.",
                pending_email=None,
                pending_token=None,
                conversation=None,
            )
            db.set_agent_status(agent_id, "idle")
            return

        if approve:
            email = email or {}
            try:
                send_gmail(email.get("to", ""), email.get("subject", ""), email.get("body", ""))
                result_text = f"Email sent to {email.get('to', '')}."
                # Record delivery immediately. If the follow-up model call then
                # fails, the user must not be told the turn failed with no trace
                # that the mail actually went — that invites a duplicate send.
                db.add_message(agent_id, "assistant", f"[Email sent to {email.get('to', '')}]")
            except Exception as exc:
                result_text = f"Failed to send email: {exc}"
        else:
            result_text = "The user declined to send this email."

        client = llm.get_client()
        outcome = llm.resume_after_confirmation(
            client,
            system,
            state["messages"],
            state["pending_tool_uses"],
            state.get("tool_use_id", ""),
            result_text,
        )
        db.update_job(job_id, pending_email=None, conversation=None)
        _finish(job_id, agent_id, outcome)
    except Exception as exc:
        db.update_job(job_id, status="error", error=str(exc))
        db.set_agent_status(agent_id, "idle")


@app.route("/jobs/<job_id>/confirm", methods=["POST"])
@login_required
def confirm_job(job_id: str):
    payload = request.get_json(silent=True) or {}

    # Must be a real JSON boolean. bool() on the raw value made every non-empty
    # string truthy, so {"approve": "false"} sent the email — fail-open on the
    # one endpoint where that means mail leaves the account.
    approve = payload.get("approve")
    if approve is not True and approve is not False:
        return jsonify({"error": "approve must be true or false"}), 400

    token = payload.get("pending_token")
    if not isinstance(token, str) or not token:
        return jsonify({"error": "pending_token is required"}), 400

    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    agent = db.get_agent(job["agent_id"])
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    # Snapshot the email that was actually approved, then claim atomically. The
    # claim is what makes this safe: it succeeds exactly once per parked message,
    # so neither a concurrent confirm nor a replayed one can send a second time.
    email = job["pending_email"]
    if not db.claim_confirmation(job_id, token):
        return jsonify({"error": "This request was already handled or has expired."}), 409

    threading.Thread(
        target=_resume_async,
        args=(job_id, job["agent_id"], agent["instructions"], approve, email),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "pending"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
