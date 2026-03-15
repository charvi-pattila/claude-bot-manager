# My Claude Agent Manager

A personal AI agent manager app built with Python + Flask.
Access it from your phone or Mac browser.

---

## How to Access
- On Mac: http://127.0.0.1:8080
- On Phone (same WiFi): http://192.168.1.76:8080

---

## Starting the App

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
- app.py — the backend server
- bots.json — stores your agents and bots
- .env — your API key (never share this!)
- templates/index.html — the app UI
- static/manifest.json — makes it installable as a phone app
- static/sw.js — service worker for PWA

---

## Installing on iPhone as an App
1. Open Safari on iPhone
2. Go to http://192.168.1.76:8080
3. Tap the Share button
4. Tap "Add to Home Screen"
5. Tap Add

---

## API Key
Stored in: /Users/charvipattila/claude-bot-manager/.env
Format: ANTHROPIC_API_KEY=your-key-here
Get/manage keys at: https://console.anthropic.com

---

## Notes
- App stops if Mac restarts (auto-start setup coming later)
- Requires Mac to be on and awake for phone access
- Both Mac and phone must be on the same WiFi
