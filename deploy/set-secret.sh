#!/bin/zsh
# Save one secret into /opt/hunt/.env on the server without it ever
# appearing on screen, in shell history, or in chat.
# Usage: deploy/set-secret.sh TELEGRAM_BOT_TOKEN
set -eu
NAME="${1:?usage: set-secret.sh NAME}"
[[ "$NAME" =~ '^[A-Z][A-Z0-9_]*$' ]] || { echo "Name must be CAPITALS_AND_UNDERSCORES."; exit 1; }
[ -f deploy/target.env ] && . deploy/target.env
HOST="${HUNT_HOST:?set HUNT_HOST in deploy/target.env}"
KEY="${HUNT_KEY:-$HOME/.ssh/id_ed25519}"

printf 'Paste %s (hidden), then Enter: ' "$NAME"
read -rs VALUE
echo
[[ -n "$VALUE" ]] || { echo "Nothing pasted, nothing saved."; exit 1; }

printf '%s=%s\n' "$NAME" "$VALUE" | ssh -i "$KEY" "$HOST" \
  "touch /opt/hunt/.env && chmod 600 /opt/hunt/.env && sed -i '/^$NAME=/d' /opt/hunt/.env && cat >> /opt/hunt/.env"
echo "Saved $NAME on server (${#VALUE} characters)."
unset VALUE
