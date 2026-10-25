#!/usr/bin/env bash
# ================================================================
# Where the server's alerts go: an email address, and the key of the mail
# service that sends them (Resend - DigitalOcean closes the SMTP ports, so
# mail leaves the droplet over HTTPS instead). Run it yourself, on the
# server, from deploy/uiu:
#
#     bash set-alerts.sh
#
# The key is typed without being shown and goes only into .env. Then the
# monitor is restarted and a test email is sent, so you know it works.
#
# Without a domain of your own verified in Resend, it sends only to the
# address the Resend account was opened with: open it with the address the
# alerts should go to.
# ================================================================
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env here - run this from deploy/uiu on the server."; exit 1; }

current=$(sed -n 's/^AIMS_ALERT_EMAIL=//p' .env | tail -n 1)
read -r -p "Send alerts to [${current:-none yet}]: " EMAIL
EMAIL="$(printf '%s' "${EMAIL:-$current}" | tr -d '[:space:]')"
case "$EMAIL" in
    ?*@?*.?*) ;;
    *) echo "That is not an email address - run it again."; exit 1 ;;
esac
KEY=""
while [ -z "$KEY" ]; do
    read -r -s -p "Resend API key (starts re_; hidden - paste once, then Enter): " KEY; echo
    KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
done
case "$KEY" in
    re_*) ;;
    *) echo "A Resend API key starts with re_ - run it again."; exit 1 ;;
esac

export EMAIL KEY
python3 - <<'PY'
import os, pathlib
env = pathlib.Path(".env")
lines = env.read_text().split(chr(10))
for name, value in (("AIMS_ALERT_EMAIL", os.environ["EMAIL"]),
                    ("AIMS_RESEND_API_KEY", os.environ["KEY"])):
    hit = False
    for i, line in enumerate(lines):
        if line.startswith(name + "="):
            lines[i] = f"{name}={value}"
            hit = True
    if not hit:
        lines.append(f"{name}={value}")
env.write_text(chr(10).join(lines).rstrip(chr(10)) + chr(10))
env.chmod(0o600)
PY
echo "Saved. Restarting the monitor and sending a test email..."
docker compose up -d --no-deps monitor </dev/null >/dev/null 2>&1
if docker compose exec -T monitor python -c "
import monitor
ok = monitor.Mailer().send('test: alerts reach you', 'This is the AIMScribe server. Alerts will come to this address: a problem when it starts, a reminder every few hours while it lasts, an all-clear when it is fixed, and a weekly report.')
raise SystemExit(0 if ok else 1)
" </dev/null; then
    echo "Test email sent to $EMAIL. If it is not in the inbox in a minute, look in spam."
else
    echo "The test email was NOT accepted. Check the key (Resend > API Keys), and that the"
    echo "Resend account was opened with $EMAIL. Then run this again."
    exit 1
fi
