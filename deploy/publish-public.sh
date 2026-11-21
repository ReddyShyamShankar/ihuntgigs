#!/usr/bin/env bash
# Copy ONLY the open-source parts of Hunt to the public GitHub repo.
# Private things (portfolio, docs, memory, CLAUDE.md, server address) never leave the private repo.
# Usage: deploy/publish-public.sh      DRY=1 lists files only; YES=1 skips the confirmation prompt.
# Needs deploy/private-patterns.txt (git-ignored): one private word/regex per line (names, server, emails...).
set -euo pipefail
[ -f deploy/target.env ] && . deploy/target.env
PUBLIC_REPO="${HUNT_PUBLIC_REPO:?set HUNT_PUBLIC_REPO=owner/repo in deploy/target.env}"
PRIVATE_PATTERNS="deploy/private-patterns.txt"
[ -s "$PRIVATE_PATTERNS" ] || { echo "Missing $PRIVATE_PATTERNS - refusing to publish."; exit 1; }
GH_ID="$(gh api user --jq .id)"; GH_LOGIN="$(gh api user --jq .login)"
NOREPLY="${GH_ID}+${GH_LOGIN}@users.noreply.github.com"
FILES=(
  hunt/__init__.py hunt/__main__.py hunt/brain.py hunt/chatgpt.py hunt/config.py
  hunt/filters.py hunt/gmail_alerts.py hunt/sources.py hunt/store.py hunt/telegram.py hunt/pick.py hunt/policy.py
  hunt/registry.py hunt/youtube.py hunt/contacts.py hunt/research.py hunt/pitch.py hunt/outreach.py hunt/mailer.py hunt/sender.py hunt/followups.py hunt/totp.py hunt/dashboard.py hunt/settings.py hunt/autoapply.py hunt/robot.py hunt/linkedin.py
  applier/hunt_applier.py applier/profile.example.yaml
  deploy/deploy.sh deploy/hunt.service deploy/hunt-dash.service deploy/hunt-robot.service deploy/hunt-robot.timer deploy/dashboard-domain.sh deploy/set-secret.sh deploy/push-login.sh deploy/publish-public.sh
  data/sources.yaml data/portfolio.example.yaml
  requirements.txt .env.example .gitignore
)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/pub"
if [ "${DRY:-0}" = 1 ]; then mkdir -p "$OUT"; else gh repo clone "$PUBLIC_REPO" "$OUT" -- -q; fi
find "$OUT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
for f in "${FILES[@]}"; do mkdir -p "$OUT/$(dirname "$f")"; git show "HEAD:$f" > "$OUT/$f"; done
chmod +x "$OUT"/deploy/*.sh
# Job-source list may name private clients (e.g. career pages): drop those lines from the public copy only.
before=$(wc -l < "$OUT/data/sources.yaml")
grep -viE -f "$PRIVATE_PATTERNS" "$OUT/data/sources.yaml" > "$OUT/data/sources.yaml.tmp" || true
mv "$OUT/data/sources.yaml.tmp" "$OUT/data/sources.yaml"
echo "sources.yaml: $((before - $(wc -l < "$OUT/data/sources.yaml"))) private line(s) left out of public copy"
git show HEAD:opensource/README.md > "$OUT/README.md"
git show HEAD:opensource/LICENSE > "$OUT/LICENSE"

# ---- Safety net: scan EVERY file (this script included) before anything is pushed ----
fail=0
scan() {  # $1 = label, rest = grep args
  local label="$1"; shift
  local hits; hits="$(grep -rInE --exclude-dir=.git "$@" "$OUT" || true)"
  if [ -n "$hits" ]; then echo "STOP ($label):"; echo "$hits" | sed "s|$OUT/||" | cut -c1-160; fail=1; fi
}
scan "private word" -i -f "$PRIVATE_PATTERNS"
scan "Telegram bot token" '[0-9]{8,10}:[A-Za-z0-9_-]{35}'
scan "API key" '(sk-[A-Za-z0-9_-]{20,}|oaiapp_[A-Za-z0-9]{6,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{10,})'
scan "JWT / token" 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
scan "private key" 'BEGIN [A-Z ]*PRIVATE KEY'
emails="$(grep -rhoE --exclude-dir=.git '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$OUT" | grep -vE '@((server\.)?example\.(com|org)|users\.noreply\.github\.com)$' || true)"
[ -z "$emails" ] || { echo "STOP (email address): $emails"; fail=1; }
ips="$(grep -rhoE --exclude-dir=.git '([0-9]{1,3}\.){3}[0-9]{1,3}' "$OUT" | grep -vE '^(127\.0\.0\.1|0\.0\.0\.0|1\.2\.3\.4|169\.254\.169\.254)$' || true)"
[ -z "$ips" ] || { echo "STOP (IP address): $ips"; fail=1; }
[ "$fail" = 0 ] || { echo "Nothing published."; exit 1; }

if [ "${DRY:-0}" = 1 ]; then (cd "$OUT" && find . -type f | sort); echo "Scan clean."; exit 0; fi
cd "$OUT"
git add -A
git diff --cached --quiet && { echo "Public repo already up to date."; exit 0; }
git diff --cached --stat
if [ "${YES:-0}" != 1 ]; then read -r -p "Publish these changes publicly? [y/N] " ok; [ "$ok" = y ] || exit 1; fi
# Commit as GitHub's private noreply address, in UTC, so no personal email or timezone is published.
TZ=UTC git -c user.name="$GH_LOGIN" -c user.email="$NOREPLY" commit -q -m "Sync from private repo"
bad="$(git log --format='%ae%n%ce' | sort -u | grep -vx "$NOREPLY" || true)"
[ -z "$bad" ] || { echo "STOP: public history has a non-noreply author: $bad"; exit 1; }
git push -q origin HEAD:main
echo "Published to https://github.com/$PUBLIC_REPO"
