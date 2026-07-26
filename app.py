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
import secrets
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
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
# A random key means sessions do not survive a restart (everyone re-logs in).
# Set SECRET_KEY to keep them across deploys.
app.secret_key = config.SECRET_KEY or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Cookie is only sent over HTTPS in deployment; relaxed locally so http://
    # localhost still works.
    SESSION_COOKIE_SECURE=bool(config.ALLOWED_ORIGINS),
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


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        supplied = (request.form.get("password") or "").strip()
        # compare_digest so a wrong password takes the same time as a right one.
        if hmac.compare_digest(supplied, config.APP_PASSWORD):
            session.clear()
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
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
        db.update_job(
            job_id,
            status="awaiting_confirmation",
            pending_email=outcome["email"],
            conversation={
                "messages": outcome["conversation"],
                "pending_tool_uses": outcome["pending_tool_uses"],
                "tool_use_id": outcome["tool_use_id"],
            },
        )
        return

    reply = outcome["text"]
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
        }
    )


def _resume_async(job_id: str, agent_id: str, system: str, approve: bool) -> None:
    job = db.get_job(job_id)
    state = job["conversation"] or {}
    try:
        if approve:
            email = job["pending_email"] or {}
            try:
                send_gmail(email.get("to", ""), email.get("subject", ""), email.get("body", ""))
                result_text = f"Email sent to {email.get('to', '')}."
            except Exception as exc:
                result_text = f"Failed to send email: {exc}"
        else:
            result_text = "The user declined to send this email."

        client = llm.get_client()
        outcome = llm.resume_after_confirmation(
            client,
            system,
            state.get("messages", []),
            state.get("pending_tool_uses", []),
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
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "awaiting_confirmation":
        return jsonify({"error": "This job is not awaiting confirmation."}), 409

    approve = bool((request.get_json(silent=True) or {}).get("approve"))
    agent = db.get_agent(job["agent_id"])
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    db.update_job(job_id, status="pending")
    threading.Thread(
        target=_resume_async,
        args=(job_id, job["agent_id"], agent["instructions"], approve),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "pending"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
