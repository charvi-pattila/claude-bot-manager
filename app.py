import os
import json
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import anthropic
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)
CORS(app)

api_key = os.getenv("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=api_key) if api_key else None
AGENTS_FILE = "bots.json"
DB_FILE = "chat_history.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def load_agents():
    with open(AGENTS_FILE, "r") as f:
        return json.load(f)


def save_agents(agents):
    with open(AGENTS_FILE, "w") as f:
        json.dump(agents, f, indent=2)


@app.route("/debug-key")
def debug_key():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return jsonify({"status": "MISSING - no key set"})
    return jsonify({
        "length": len(key),
        "starts_with": key[:12],
        "ends_with": key[-4:],
        "has_spaces": " " in key,
        "has_newline": "\n" in key or "\r" in key
    })


@app.route("/test-api")
def test_api():
    try:
        if not client:
            return jsonify({"error": "client not initialized - no API key"})
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}]
        )
        return jsonify({"status": "ok", "reply": r.content[0].text})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()})


@app.route("/")
def index():
    resp = render_template("index.html")
    from flask import make_response
    r = make_response(resp)
    r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return r


@app.route("/agents", methods=["GET"])
def get_agents():
    return jsonify(load_agents())


@app.route("/agents", methods=["POST"])
def create_agent():
    data = request.json
    agents = load_agents()
    agent = {
        "id": str(len(agents) + 1),
        "name": data["name"],
        "type": data.get("type", "bot"),
        "instructions": data.get("instructions", ""),
        "status": "idle",
        "created_at": datetime.now().strftime("%H:%M")
    }
    agents.append(agent)
    save_agents(agents)
    return jsonify(agent)


@app.route("/agents/<agent_id>", methods=["DELETE"])
def delete_agent(agent_id):
    agents = load_agents()
    agents = [a for a in agents if a["id"] != agent_id]
    save_agents(agents)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM messages WHERE agent_id = ?", (agent_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Deleted"})


@app.route("/agents/<agent_id>/status", methods=["POST"])
def update_status(agent_id):
    data = request.json
    agents = load_agents()
    for a in agents:
        if a["id"] == agent_id:
            a["status"] = data["status"]
    save_agents(agents)
    return jsonify({"message": "Updated"})


@app.route("/agents/<agent_id>/history", methods=["GET"])
def get_history(agent_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE agent_id = ? ORDER BY id",
        (agent_id,)
    ).fetchall()
    conn.close()
    return jsonify([{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows])


@app.route("/agents/<agent_id>/history", methods=["DELETE"])
def clear_history(agent_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM messages WHERE agent_id = ?", (agent_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Cleared"})


EMAIL_TOOLS = [
    {
        "name": "send_email",
        "description": "Send an email via Gmail on behalf of the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"}
            },
            "required": ["to", "subject", "body"]
        }
    }
]


def send_gmail(to, subject, body):
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_password:
        raise ValueError("Gmail credentials not configured")

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, to, msg.as_string())


@app.route("/chat/<agent_id>", methods=["POST"])
def chat(agent_id):
    try:
        agents = load_agents()
        agent = next((a for a in agents if a["id"] == agent_id), None)
        if not agent:
            return jsonify({"error": "Agent not found"}), 404

        for a in agents:
            if a["id"] == agent_id:
                a["status"] = "running"
        save_agents(agents)

        data = request.json
        message = data.get("message", "")

        # Save user message
        now = datetime.now().strftime("%H:%M")
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO messages (agent_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, "user", message, now)
        )
        conn.commit()

        # Load full history for context
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE agent_id = ? ORDER BY id",
            (agent_id,)
        ).fetchall()
        conn.close()

        history = [{"role": r[0], "content": r[1]} for r in rows]

        kwargs = {
            "model": "claude-opus-4-6",
            "max_tokens": 1024,
            "messages": history,
            "tools": EMAIL_TOOLS
        }
        if agent.get("instructions"):
            kwargs["system"] = agent["instructions"]

        if not client:
            return jsonify({"error": "ANTHROPIC_API_KEY not configured on server"}), 500

        response = client.messages.create(**kwargs)

        # Handle tool use
        reply = None
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "send_email":
                    try:
                        send_gmail(block.input["to"], block.input["subject"], block.input["body"])
                        result = f"Email sent to {block.input['to']}"
                    except Exception as e:
                        result = f"Failed to send email: {str(e)}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            # Convert SDK content blocks to plain dicts for the follow-up call
            assistant_content = []
            for block in response.content:
                if block.type == "tool_use":
                    assistant_content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                elif block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})

            # Send tool results back to Claude for final reply
            history.append({"role": "assistant", "content": assistant_content})
            history.append({"role": "user", "content": tool_results})
            kwargs["messages"] = history
            follow_up = client.messages.create(**kwargs)
            reply = follow_up.content[0].text
        else:
            reply = response.content[0].text

        # Save assistant reply
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO messages (agent_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, "assistant", reply, datetime.now().strftime("%H:%M"))
        )
        conn.commit()
        conn.close()

        for a in agents:
            if a["id"] == agent_id:
                a["status"] = "idle"
        save_agents(agents)

        return jsonify({"reply": reply})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500


@app.route("/direct", methods=["POST"])
def direct_chat():
    data = request.json
    history = data.get("history", [])
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=history
    )
    return jsonify({"reply": response.content[0].text})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
