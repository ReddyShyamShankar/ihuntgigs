#!/usr/bin/env bash
# Deploy Hunt to your server. Touches ONLY /opt/hunt, the hunt user and hunt.service.
# Usage: deploy/deploy.sh            (from the repo root)
set -euo pipefail
# Server address lives in deploy/target.env (git-ignored): HUNT_HOST=root@1.2.3.4  HUNT_KEY=~/.ssh/key
[ -f deploy/target.env ] && . deploy/target.env
HOST="${HUNT_HOST:?set HUNT_HOST in deploy/target.env}"
KEY="${HUNT_KEY:-$HOME/.ssh/id_ed25519}"
SSH="ssh -i $KEY -o BatchMode=yes"
# Ship only what the bot needs to run (no notes, docs, secrets or runtime data).
rsync -az --delete --no-owner --no-group -e "$SSH" \
  --exclude '/deploy/target.env' --exclude '__pycache__' --exclude '*.pyc' \
  --include '/hunt/***' --include '/deploy/***' --include '/requirements.txt' \
  --include '/data/' --include '/data/sources.yaml' --include '/data/portfolio.yaml' \
  --exclude '*' \
  ./ "$HOST:/opt/hunt/"
$SSH "$HOST" 'set -e
  cd /opt/hunt
  rm -rf -- memory docs opensource CLAUDE.md README.md .gitignore .env.example   # private notes never belong on the server
  id hunt >/dev/null 2>&1 || useradd --system --home-dir /opt/hunt --no-create-home --shell /usr/sbin/nologin hunt
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  touch .env && chown root:root .env && chmod 600 .env
  # Code is root-owned and read-only to the bot; only data/ belongs to the hunt user.
  chown -R root:root /opt/hunt && chmod 755 /opt/hunt
  chown hunt:hunt data && chmod 700 data
  find data -maxdepth 1 -type f ! -name "*.yaml" -exec chown hunt:hunt {} + -exec chmod 600 {} +
  chmod 644 data/*.yaml
  cp deploy/hunt.service /etc/systemd/system/hunt.service
  systemctl daemon-reload
  systemctl enable hunt >/dev/null 2>&1 || true
  if grep -q "^TELEGRAM_BOT_TOKEN=." .env && { [ -f data/chatgpt.json ] || grep -q "^ANTHROPIC_API_KEY=." .env; }; then
    systemctl restart hunt && sleep 5 && systemctl --no-pager --lines=5 status hunt
  else
    echo "Installed. Waiting for Telegram token + AI sign-in (push-login.sh) before starting."
  fi'
