# Hunt — a lead finder for freelancers

Hunt watches public news and job feeds for businesses that are about to need a freelancer
(just raised money, hiring for a role you can cover on contract, asking for help), scores each
lead with AI, drafts a short pitch that cites only your real past work, and sends it to your
Telegram. **It never sends anything by itself — you review and send.**

Built for one person, run on your own small server.

## How it works
```
data/sources.yaml → collectors (RSS/JSON, no scraping, no logins) → keyword filter → SQLite queue
queue → AI (score 0-100 + email + DM, max N/day) → Telegram card with buttons
your taps (Sent / Skip / Replied / Won / Lost) → weekly per-source digest
```

## AI: use your ChatGPT plan (or an Anthropic API key)
- **Default — Sign in with ChatGPT.** Uses OpenAI's official
  [Sign in with ChatGPT](https://developers.openai.com/siwc) plan-usage flow for open-source,
  self-hosted apps. No API key. Usage counts against your ChatGPT plan; check it at
  chatgpt.com/settings/usage.
- **Alternative — Claude.** Set `HUNT_AI=claude` and `ANTHROPIC_API_KEY` in `.env`.

## Setup
1. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. `cp data/portfolio.example.yaml data/portfolio.yaml` and describe your real work there.
   The AI may cite only what is in this file.
3. Edit `data/sources.yaml` for your niche.
4. Create a Telegram bot with @BotFather; put its token in `.env` (see `.env.example`).
5. `.venv/bin/python -m hunt login` — signs in with ChatGPT in your browser.
6. Try it: `.venv/bin/python -m hunt collect` (free dry run), then `.venv/bin/python -m hunt run`.

## Run it on a server
- Put `HUNT_HOST=root@server.example.com` and `HUNT_KEY=~/.ssh/yourkey` in `deploy/target.env`.
- `deploy/deploy.sh` installs a systemd service in `/opt/hunt`.
- `deploy/set-secret.sh TELEGRAM_BOT_TOKEN` saves a secret on the server without showing it.
- `deploy/push-login.sh` moves your ChatGPT sign-in to the server (OpenAI's self-hosted VM flow);
  the server then refreshes it on its own.

## Commands
| Command | What it does |
|---|---|
| `python -m hunt collect` | Fetch + filter, print what would go to the AI (no cost) |
| `python -m hunt run` | One full cycle |
| `python -m hunt bot` | Always on: hourly cycles + Telegram buttons |
| `python -m hunt login` / `models` | ChatGPT sign-in / list your plan's models |

Telegram: `/start` shows your chat id, `/run` runs a cycle, `/stats` shows results.
Reply to a draft with an instruction ("shorter") and Hunt rewrites it.

MIT licensed.
