#!/bin/sh
# Container entrypoint for the Kingshot bot fork.
#
# main.py reads the Discord token from bot_token.txt (upstream convention), so we
# materialise that file from the environment before starting. We prefer the
# upstream DISCORD_BOT_TOKEN name and fall back to DISCORD_TOKEN (the .env this
# fork was ported with). An existing mounted bot_token.txt still wins when
# neither env var is set.
set -e

cd /app

TOKEN="${DISCORD_BOT_TOKEN:-$DISCORD_TOKEN}"
if [ -n "$TOKEN" ]; then
    printf '%s' "$TOKEN" > bot_token.txt
fi

# This fork does not publish GitHub releases, so always skip the release-based
# update check (which would otherwise fail at startup).
exec python main.py --no-update
