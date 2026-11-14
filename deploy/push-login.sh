#!/usr/bin/env bash
# Move your ChatGPT sign-in from this computer to the server (OpenAI "self-hosted VM" flow).
# Run after `python -m hunt login`. The server then owns token refreshes, so the tokens are
# removed from this computer afterwards (the app registration id is kept for future logins).
set -euo pipefail
[ -f deploy/target.env ] && . deploy/target.env
HOST="${HUNT_HOST:?set HUNT_HOST in deploy/target.env}"
KEY="${HUNT_KEY:-$HOME/.ssh/id_ed25519}"
SSH="ssh -i $KEY -o BatchMode=yes"
CREDS=data/chatgpt.json
[ -f "$CREDS" ] || { echo "No $CREDS. Run: .venv/bin/python -m hunt login"; exit 1; }

$SSH "$HOST" 'umask 077; mkdir -p /opt/hunt/data && cat > /opt/hunt/data/chatgpt.json.new' < "$CREDS"
$SSH "$HOST" 'set -e; cd /opt/hunt
  [ -s data/host_id ] || echo "urn:uuid:$(cat /proc/sys/kernel/random/uuid)" > data/host_id
  python3 - <<EOF
import fcntl, json, os
with open("data/chatgpt.lock", "a") as lock:          # same lock the running bot uses
    fcntl.flock(lock, fcntl.LOCK_EX)
    p = "data/chatgpt.json.new"
    c = json.load(open(p))
    c["ext_agent_host_id"] = open("data/host_id").read().strip()  # keep the server its own host id
    with open(p, "w") as f:
        json.dump(c, f, indent=2); f.flush(); os.fsync(f.fileno())
    os.chmod(p, 0o600)
    os.replace(p, "data/chatgpt.json")
EOF
  if id hunt >/dev/null 2>&1; then chown hunt:hunt data/chatgpt.json data/chatgpt.lock data/host_id; fi
  echo "Server now holds the ChatGPT sign-in."'

.venv/bin/python - <<'EOF'
import json, os
p = "data/chatgpt.json"
c = json.load(open(p))
for k in ("access_token", "refresh_token"):
    c[k] = ""
json.dump(c, open(p, "w"), indent=2)
os.chmod(p, 0o600)
print("Removed tokens from this computer (server refreshes them from now on).")
EOF
