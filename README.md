# My Claude Agent Manager

A personal AI agent manager app built with Python + Flask.
Access it from your phone or Mac browser.

---

## Live URL (Railway)
https://web-production-39cda6.up.railway.app/

---

## How to Access (Local)
- On Mac: http://127.0.0.1:8080
- On Phone (same WiFi): http://192.168.1.76:8080

---

## Starting the App (Local)

### Run in background (closes with terminal safely):
```
cd /Users/charvipattila/claude-bot-manager && source venv/bin/activate && nohup python3 app.py > app.log 2>&1 &
```

### Run normally (for testing):
```
cd /Users/charvipattila/claude-bot-manager
source venv/bin/activate
python3 app.py
```

---

## Stopping the App
```
pkill -f app.py
```

---

## Checking if it's Running
```
curl http://127.0.0.1:8080
```

---

## Checking for Errors
```
cat /Users/charvipattila/claude-bot-manager/app.log
```

---

## Project Files
- app.py — the backend server (with SQLite chat history + Gmail email tool)
- bots.json — stores your agents and bots
- chat_history.db — SQLite database for persistent chat history (auto-created)
- .env — your API keys (never share this!)
- templates/index.html — the app UI (dark pink theme)
- static/manifest.json — makes it installable as a phone app
- static/sw.js — service worker for PWA
- Dockerfile — used for Railway deployment
- requirements.txt — Python dependencies

---

## Features
- Dark pink UI
- Create Claude agents and bots
- Persistent chat history (synced across devices via server)
- Bots can send Gmail emails via Claude tool use

---

## Railway Deployment (IN PROGRESS)
Deployed at: https://web-production-39cda6.up.railway.app/

### Railway Variables (already set):
- `ANTHROPIC_API_KEY` — Anthropic API key
- `GMAIL_USER` — Gmail address for sending emails
- `GMAIL_APP_PASSWORD` — Gmail App Password (16 chars from Google Account > Security > App Passwords)

### Current Issue:
Railway is crashing with `$PORT is not a valid port number`.
The Dockerfile hardcodes port 8080 but Railway may have a custom Start Command overriding it.

**Next step to fix:** In Railway → service → Settings → check for a "Start Command" field and clear it if it contains `gunicorn app:app --bind 0.0.0.0:$PORT`.

---

## Installing on iPhone as an App
1. Open Safari on iPhone
2. Go to https://web-production-39cda6.up.railway.app/
3. Tap the Share button
4. Tap "Add to Home Screen"
5. Tap Add

---

## API Keys
- Anthropic: https://console.anthropic.com
- Gmail App Password: Google Account > Security > 2-Step Verification > App Passwords

---

## Notes
- Local app stops if Mac restarts
- Railway deployment is always online (once fixed)
