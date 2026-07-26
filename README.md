# Claude Bot Manager

A personal web app for creating your own Claude-powered assistants. Give each
bot a name and a set of instructions, chat with it, and it remembers the
conversation. Bots can also draft emails and send them from your Gmail — but
only after you read the message and press Send.

Runs on your laptop or on a host like Railway, and installs to an iPhone home
screen as a web app.

---

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/charvi-pattila/claude-bot-manager.git
cd claude-bot-manager

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env — see Configuration below
python app.py
```

Open http://127.0.0.1:8080 and log in with the `APP_PASSWORD` you set.

At minimum `.env` needs:

```
APP_PASSWORD=pick-a-long-password
ANTHROPIC_API_KEY=sk-ant-...
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
```

Get an API key at [console.anthropic.com](https://console.anthropic.com).

---

## Configuration

Everything is read from the environment (or `.env`). Full template in
[`.env.example`](.env.example).

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `APP_PASSWORD` | **yes** | — | Login password. The app won't start without it. |
| `ANTHROPIC_API_KEY` | **yes** | — | Your Anthropic API key. |
| `SECRET_KEY` | **yes** | — | Signs the login cookie. Required: the app runs 2 workers, and without a fixed key each signs differently, logging you out at random. |
| `DATABASE_URL` | in deployment | `sqlite:///chat_history.db` | Where agents and messages live. **See the deployment warning below.** |
| `GMAIL_USER` | for email | — | Gmail address bots send from. |
| `GMAIL_APP_PASSWORD` | for email | — | Google Account → Security → 2-Step Verification → App Passwords. |
| `GMAIL_ALLOWED_RECIPIENTS` | no | unset | Comma-separated allowlist. If set, email can only go to these addresses. |
| `CHAT_MODEL` | no | `claude-opus-5` | Model to use. |
| `EFFORT` | no | `medium` | `low`–`max`. Higher means more thinking, more tokens, slower. |
| `MAX_TOKENS` | no | `8192` | Cap on a single reply, thinking included. |
| `HISTORY_LIMIT` | no | `40` | Turns of history re-sent per message. |
| `MAX_TOOL_ROUNDS` | no | `6` | Ceiling on tool calls in one turn. |
| `ALLOWED_ORIGINS` | no | unset | Comma-separated origins allowed to call the API cross-origin. |
| `SECURE_COOKIES` | in deployment | `false` | Send the login cookie over HTTPS only. |
| `SESSION_DAYS` | no | `7` | How long a login lasts. |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_S` | no | `10` / `60` | Wrong passwords allowed per address per window before 429. |
| `PORT` | no | `8080` | Port to listen on. |

---

## How it works

```
templates/  index.html   the UI (agent list + chat)
            login.html   password screen
app.py      routes, login, and the chat job lifecycle
llm.py      Anthropic client and the tool-use loop
db.py       agents, messages, and jobs  (SQLite or Postgres)
config.py   every setting, read once at startup
```

A chat message takes this path:

1. The browser posts to `/chat/<agent_id>`, which saves your message and starts
   a background job, returning a `job_id` right away.
2. The browser polls `/poll/<job_id>` until the job finishes. Returning
   immediately is what keeps a long reply from tripping a host's request timeout.
3. The job asks Claude for a reply, looping if Claude wants to use a tool.
4. If Claude wants to send an email, the job **stops** and waits. You get a card
   showing exactly what would be sent, with Send and Don't send. Each parked
   message gets a single-use token that your confirmation has to quote, so an
   approval can only ever apply to the message you were actually shown.

Jobs live in the database rather than in memory, so a reply survives a restart
and works when more than one server process is running.

---

## Email safety

A bot can propose an email; it can never send one by itself.

Bot instructions are free text, and text can be written to talk a model into
doing something you didn't intend. Since a bot can reach your real Gmail, the
confirmation step is what stands between a bad instruction and a real message
leaving your account. Three things guard it:

1. **Login.** The app refuses to start without `APP_PASSWORD`, so an instance
   left on the internet isn't open to whoever finds the URL.
2. **Confirmation.** Every send shows the recipient, subject, and body first.
3. **Allowlist** (optional). Set `GMAIL_ALLOWED_RECIPIENTS` and mail can only go
   to addresses you listed, even if you click Send by mistake.

---

## Deploying

The included `Dockerfile` works on Railway, Render, Fly, or anything that runs
containers. Set the same environment variables from the table above in your
host's dashboard.

> **Set `DATABASE_URL` to a Postgres database.**
>
> Container filesystems are wiped on every redeploy. With the default SQLite
> file, every agent and every message disappears each time you deploy. On
> Railway: add a Postgres service, then copy its connection string into
> `DATABASE_URL`. (`postgres://` URLs are handled automatically.)

Health check endpoint: `/healthz`.

### Install on iPhone

1. Open the deployed URL in Safari
2. Share → **Add to Home Screen** → Add

---

## Development

```bash
pip install -r requirements-dev.txt

pytest                 # tests — no network, no API credits, no real email
ruff check .           # lint
ruff format .          # format
```

The tests stub out both Claude and SMTP, so they're safe to run anytime. CI runs
the same three commands on every push and pull request.

---

## License

Personal project — no license specified.
