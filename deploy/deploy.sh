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
  --include '/applier/' --include '/applier/hunt_applier.py' \
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
  # Server robot (route 1): a hidden browser in its own capped service. Browser files live in /opt/hunt/browsers.
  .venv/bin/pip install -q "playwright>=1.65,<2"
  [ -d browsers ] || PLAYWRIGHT_BROWSERS_PATH=/opt/hunt/browsers .venv/bin/python -m playwright install --with-deps chromium >/dev/null
  chmod -R a+rX browsers
  install -d -o hunt -g hunt -m 700 data/robot-home data/robot-runs data/private
  chown -R hunt:hunt data/robot-home data/robot-runs data/private   # the chown -R root above resets these
  chmod 644 data/*.yaml
  cp deploy/hunt.service /etc/systemd/system/hunt.service
  cp deploy/hunt-dash.service /etc/systemd/system/hunt-dash.service   # dashboard: 127.0.0.1 only
  cp deploy/hunt-robot.service deploy/hunt-robot.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable hunt hunt-dash >/dev/null 2>&1 || true
  if [ -f data/private/profile.yaml ]; then systemctl enable --now hunt-robot.timer >/dev/null 2>&1; echo "robot timer: $(systemctl is-active hunt-robot.timer)"
  else echo "Robot off: no data/private/profile.yaml yet (jobs go to the Mac helper)."; fi
  systemctl restart hunt-dash && sleep 2 && echo "dashboard: $(systemctl is-active hunt-dash)"
  if grep -q "^TELEGRAM_BOT_TOKEN=." .env; then   # AI may be off (HUNT_AI); Hunt still finds jobs
    systemctl restart hunt && sleep 5 && systemctl --no-pager --lines=5 status hunt
  else
    echo "Installed. Waiting for Telegram token before starting."
  fi'
