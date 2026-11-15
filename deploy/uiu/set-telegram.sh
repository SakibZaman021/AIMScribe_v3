#!/usr/bin/env bash
# ================================================================
# Telegram alerts for data collectors (037). Run it yourself, on the
# server, from deploy/uiu:
#
#     ./set-telegram.sh
#
# It asks for the bot token that @BotFather gave you (typed without being
# shown), saves it only in .env, restarts the API, and asks Telegram whether
# the token works - so you know at once.
#
# The token is a password for the bot: never send it by email or chat.
# ================================================================
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env here - run this from deploy/uiu on the server."; exit 1; }

TOKEN=""
while [ -z "$TOKEN" ]; do
    read -r -s -p "Bot token from @BotFather (hidden - paste once, then Enter): " TOKEN; echo
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
done
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'; then
    echo "That does not look like a bot token (numbers, a colon, then about 35 letters)."
    echo "Copy it again from @BotFather and run this again."
    exit 1
fi

export TOKEN
python3 - <<'PY'
import os, pathlib
env = pathlib.Path(".env")
lines = env.read_text().split(chr(10))
name, value = "AIMS_TELEGRAM_BOT_TOKEN", os.environ["TOKEN"]
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
echo "Saved. Restarting the API (about half a minute)..."
docker compose up -d --no-deps api </dev/null >/dev/null 2>&1

check() {
    docker compose exec -T api python -c "
import os, httpx
token = os.environ.get('AIMS_TELEGRAM_BOT_TOKEN', '')
r = httpx.get('https://api.telegram.org/bot' + token + '/getMe', timeout=15)
body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
if r.status_code == 200 and body.get('ok'):
    print('@' + body['result']['username'])
else:
    print('REFUSED ' + str(body.get('description') or r.status_code))
    raise SystemExit(2)
" </dev/null 2>/dev/null
}

for _ in $(seq 1 20); do
    if answer=$(check); then
        echo "Telegram works: the bot is $answer."
        echo "Now each data collector opens the dashboard on their phone, My shift,"
        echo "presses Connect Telegram, then Start in Telegram."
        exit 0
    fi
    case "$answer" in
        REFUSED*) echo "Telegram refused the token (${answer#REFUSED }). Copy it again from"
                  echo "@BotFather (/mybots > the bot > API Token) and run this again."
                  exit 1 ;;
    esac
    sleep 3
done
echo "The API did not answer in a minute. Check it with: docker compose ps api"
exit 1
