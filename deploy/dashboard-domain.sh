#!/usr/bin/env bash
# Put the Hunt dashboard on its own domain with HTTPS (Caddy gets the certificate by itself).
# Usage: deploy/dashboard-domain.sh dash.example.com     (point the domain's A record at the server first)
#
# Touches ONLY: /etc/caddy/hunt.caddy (new), one `import` line in /etc/caddy/Caddyfile (backed up first),
# HUNT_DASH_HTTPS in /opt/hunt/.env, and the hunt-dash service. Other sites in the Caddyfile are left as
# they are; if Caddy rejects the new config, everything is put back and nothing is reloaded.
set -euo pipefail
DOMAIN="${1:?usage: deploy/dashboard-domain.sh dash.example.com}"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "Not a domain name: $DOMAIN"; exit 1; }
[ -f deploy/target.env ] && . deploy/target.env
HOST="${HUNT_HOST:?set HUNT_HOST in deploy/target.env}"
KEY="${HUNT_KEY:-$HOME/.ssh/id_ed25519}"
SSH="ssh -i $KEY -o BatchMode=yes"

$SSH "$HOST" "DOMAIN='$DOMAIN' bash -s" <<'REMOTE'
set -euo pipefail
cf=/etc/caddy/Caddyfile site=/etc/caddy/hunt.caddy stamp=$(date +%s)
cp "$cf" "$cf.bak.hunt.$stamp"
[ -f "$site" ] && cp "$site" "$site.bak.$stamp"
cat > "$site" <<EOF
# Hunt dashboard — managed by Hunt's deploy/dashboard-domain.sh
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8090
}
EOF
grep -qxF "import $site" "$cf" || printf '\nimport %s\n' "$site" >> "$cf"
if ! caddy validate --config "$cf" --adapter caddyfile >/tmp/hunt-caddy-check 2>&1; then
  echo "Caddy rejected the config — putting everything back:"; tail -5 /tmp/hunt-caddy-check
  cp "$cf.bak.hunt.$stamp" "$cf"
  if [ -f "$site.bak.$stamp" ]; then cp "$site.bak.$stamp" "$site"; else rm -f "$site"; fi
  exit 1
fi
systemctl reload caddy
env=/opt/hunt/.env
grep -q '^HUNT_DASH_HTTPS=' "$env" && sed -i 's/^HUNT_DASH_HTTPS=.*/HUNT_DASH_HTTPS=1/' "$env" || echo 'HUNT_DASH_HTTPS=1' >> "$env"
systemctl restart hunt-dash
echo "Dashboard: https://$DOMAIN  (certificate is fetched on the first visit; give it a minute)"
REMOTE
