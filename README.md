# :crown: Kingshot Discord Bot

Kingshot Discord Bot that supports alliance management, event reminders and attendance tracking, gift code redemption, minister appointment planning and more. This bot is free, open source and self-hosted. It is specifically designed for [Kingshot](https://www.centurygames.com/games/kingshot/).

## 🚀 Getting Started

### Fork configuration

This fork is designed to be run only with Docker Compose.
To not modify already existing set up I added new `docker-compose.yaml` file in root together with `.env.example`.

Copy `.env.example` to `.env` beside your Compose file and fill in your Discord
bot token. Add a MightPulse API key and the relevant role/channel IDs to enable
the fork's verification and NAP features. The template documents supported
variables and their defaults; never commit your populated `.env`.

Keep local Compose changes (such as host volume paths) in a
`docker-compose.override.yaml` file beside the root `docker-compose.yaml`
instead of editing the tracked file. Docker Compose automatically merges this
override when you run `docker compose` from the repository root. The override
is already ignored by Git, so your local settings will not block automated
updates using `git pull`.

For example, customize the persistent storage paths with:

```yaml
services:
  kingshot-bot:
    volumes:
      - /srv/kingshot-bot/log:/app/log
      - /srv/kingshot-bot/db:/app/db
      - /srv/kingshot-bot/backups:/app/backups
```

To update the source and rebuild/restart the bot while retaining your local
override and `.env` settings, run:

```sh
git pull --ff-only && docker compose up -d --build
```

Extension role/channel, kingdom and NAP settings are **first-run defaults**.
Once seeded into `db/settings.sqlite`, change them using `/admin_setting`, not
by editing `.env`. `ALLOWED_KINGDOM_ID` seeds the `whitelist_kids` JSON list.
The API key and cache settings are read at startup, so restart after changing
them. For non-Docker launches, use `bot_token.txt` for the Discord token and
export any core/OCR environment overrides before starting Python.

To get started with the bot, head over to the [wiki](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki) for instructions and other information.

If you have any issues with the bot, head over to the [common issues](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki/Getting-Help) page or join our [discord server](https://discord.gg/apYByj6K2m) for support.
