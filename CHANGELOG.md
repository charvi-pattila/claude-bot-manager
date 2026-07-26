# Changelog

## Unreleased — review fixes

Independent review of the hardening pass below found bugs that the original 46
tests did not catch, because those tests only covered defects already known to
their author. Every item here now has a regression test, and each of those tests
was mutation-checked: the fix was reverted to confirm the test actually fails.

### Agent-breaking
- **An empty model reply no longer gets stored.** If thinking consumed the whole
  `MAX_TOKENS` budget the assistant message was saved as `""`; the API rejects
  empty content, so every later turn failed and — with no way to clear history in
  the UI — the agent was permanently unusable. It now reports a clear error.
- **The history window always starts with a user message.** Taking "the newest N"
  of an alternating conversation lands on an assistant message for half of all
  history lengths, and the API rejects a conversation that opens with one. Every
  agent broke for good once it passed `HISTORY_LIMIT` turns.

### Email gate
The gate was bypassable three independent ways, all now closed and tested:
- **Approval is bound to the message, not the job.** Each parked email carries a
  single-use token the client must echo back. Previously a replayed confirmation
  approved whatever happened to be parked when it arrived — and since a resumed
  turn can park *again* with a different email, a stale confirm could send a
  message the user had never seen.
- **Confirmation is claimed atomically** with a conditional `UPDATE`. Two
  simultaneous confirms both used to pass the status check and send the email
  twice; an in-process lock could not have fixed it, since the workers are
  separate processes.
- **`approve` must be a real JSON boolean.** It was parsed with `bool()`, so
  `{"approve": "false"}` — any non-empty string — sent the email.

### Other fixes
- The irreversible send now happens only *after* the resume state is validated,
  and delivery is recorded immediately, so a later failure can't leave the user
  thinking no mail went out.
- A job whose agent was deleted mid-flight no longer strands the turn as
  `pending` forever.
- The legacy `bots.json` import is guarded by a migration marker rather than "is
  the agents table empty", so deleting every agent and restarting no longer
  resurrects them. Malformed rows are skipped instead of crash-looping the
  workers at boot, and `bots.json` is now in `.dockerignore`.
- **`SECRET_KEY` is required.** With 2 workers and a per-process random key, each
  worker signed cookies differently and logins were rejected at random. It was
  documented as optional.
- `/login` is rate-limited per address; non-ASCII passwords no longer 500.
- The session cookie's `Secure` flag has its own setting instead of being
  inferred from the unrelated CORS config, and sessions expire in 7 days rather
  than Flask's 31-day default.
- Email recipients are validated as exactly one address.
- Removed a vacuous refusal test and some dead code.

## Earlier in this branch — hardening pass

Security, durability, and correctness work, plus the first tests and CI.

### Security
- **Added login.** Every route now requires a password (`APP_PASSWORD`). The app
  refuses to start without one — it can send email on your behalf, so an
  instance without a password is open to anyone who finds the URL.
- **Email now requires confirmation.** A bot proposes an email; you see the
  recipient, subject, and body, and nothing is sent until you press Send.
  Optional `GMAIL_ALLOWED_RECIPIENTS` allowlist on top.
- **Removed `/debug-key`**, which published API key length and the first and last
  characters of the key to anyone who asked.
- **Removed `/test-api`** (unauthenticated endpoint that spent API credits) and
  **`/direct`** (unauthenticated, unscoped passthrough to the model).
- **Restricted CORS** to `ALLOWED_ORIGINS` instead of allowing every website.
- **Fixed an XSS hole** in the agent list: agent names were concatenated into
  `innerHTML` and into an `onclick` attribute, so a name containing quotes and a
  `<script>` tag would execute. The list is now built as DOM nodes.
- Container runs as a non-root user; added `.dockerignore` so `.env`, `.git`,
  and the local database can't be baked into an image.

### Durability
- **Agents, messages, and jobs moved into a database** (SQLite locally, Postgres
  via `DATABASE_URL`). Previously agents lived in `bots.json` and chat history in
  a local SQLite file — both on the container filesystem, which is wiped on every
  redeploy, so all data silently vanished each time. Existing `bots.json` agents
  are imported automatically on first boot.
- **In-flight chat jobs moved out of process memory.** The old in-memory dict grew
  without bound and was invisible to other server processes, so with more than
  one worker a poll could hit a process that had never heard of the job. The
  container now runs 2 workers safely.

### Correctness
- **Agent IDs are UUIDs.** They were `str(len(agents) + 1)`, so deleting an agent
  and creating another produced a duplicate ID — and the new agent inherited the
  old one's chat history.
- **The tool loop handles multiple rounds.** It previously ran exactly once, so a
  follow-up tool call was silently dropped and the reply came back confusing.
- **Replies read every text block** instead of assuming `content[0]` is text,
  which breaks on current models where the first block is often something else.
- **History sent per request is capped** (`HISTORY_LIMIT`, default 40 turns). The
  whole conversation used to be re-sent — and re-billed — on every message.
- Requests validate input and return proper status codes (400 / 404 / 409 / 503)
  instead of raising 500s. A missing API key is now a clean error, not a crash.
- Agents no longer get stuck showing "running" after a failed turn.

### Models and dependencies
- Default model is now `claude-opus-5`, configurable via `CHAT_MODEL`, with an
  `EFFORT` dial for the cost/latency tradeoff. (The previous `claude-sonnet-4-6`
  and `claude-opus-4-6` are still valid model IDs — just a generation behind.)
- `anthropic` 0.84 → 0.120, `gunicorn` 21 → 26, plus SQLAlchemy and psycopg.

### Project scaffolding
- Added a test suite (46 tests) covering auth, agent CRUD, the ID-collision
  regression, history scoping and capping, the tool loop, and the email
  confirmation gate. Claude and SMTP are stubbed, so tests spend nothing and
  send nothing.
- Added GitHub Actions CI running `ruff check`, `ruff format --check`, `pytest`.
- Added `.env.example`, `pyproject.toml`, `requirements-dev.txt`.
- Rewrote `README.md` as a README (the changelog-style content became this file).
- Removed `static/sw.js`, a service worker that only re-issued each request and
  provided no offline behaviour. Home-screen install still works.

### Known follow-ups
- **Streaming replies.** Streaming was tried earlier and reverted because of host
  request timeouts. It's a better fix for that than polling, but it interacts
  awkwardly with the email confirmation pause, so it's left for a focused change.
  The concrete bugs the polling design caused are fixed above.
- Single shared password rather than user accounts — proportionate for a personal
  app, insufficient if it is ever shared.

---

## Earlier history

Reconstructed from commit history, newest first.

- Replaced SSE streaming with a background thread and polling to work around a
  60-second host timeout
- Switched chat to SSE streaming (superseded above)
- Increased gunicorn timeout to 120s; switched to Sonnet 4.6
- Stripped whitespace from the API key to fix a newline picked up from the
  Railway dashboard
- Added `/test-api` and `/debug-key` diagnostic endpoints (both removed above)
- Wrapped the chat endpoint in try/except to surface real errors
- Prevented a crash when `ANTHROPIC_API_KEY` was missing
- Added the 3-dot delete menu, the Gmail tool, and error handling
- Several rounds of Dockerfile and `$PORT` fixes for Railway
- Added persistent chat history in SQLite
- Added the Gmail `send_email` tool
- Initial commit: Flask agent manager with dark pink UI and chat history
