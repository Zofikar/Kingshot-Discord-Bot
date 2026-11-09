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

### Council ranked voting

Ranked proposals use **instant-runoff voting (IRV)**: alliances rank options best
to worst, and the lowest option is eliminated each round, transferring ballots
to their next remaining preference until an option has a strict majority.
Standard Yes/No/Abstain voting remains unchanged and is the default.

Create one with `/vote_submit`, choosing `vote_type: Ranked` and entering options
such as `8 | 10 | 12`. **Rank** opens a private draft showing ranked and unranked
answers separately. Select an answer, use **Rank / Unrank** and **Move up / Move
down**, then **Submit ranking**. Existing explicit preferences load when editing;
opening, editing or cancelling never changes a ballot or records participation.
Use the separate **Abstain** control to abstain explicitly.

This bot adds the following custom rules to IRV:
- **Partial rankings:** missing options follow explicit choices, ordered by other
  ranked voters' average preferences. Omitted options are treated as tied across
  unused positions when calculating those reference averages; equal completion
  averages fall back to published option order. Leaving options unranked delegates
  their relative order to other voters.
- **Tiebreaking:** fixed average ranks across completed ballots break elimination
  and final-round ties. If those averages also tie, the result is **TIED**.
- **Council participation:** one replaceable ballot per alliance; 45% quorum counts
  rankings and explicit abstentions, but not missing ballots. Abstentions never
  affect preferences or the winning majority.
- **Existing lifecycle:** 24-hour voting, audited manual corrections, admin-confirmed
  finalization and queue progression remain. Early conclusion requires explicit
  first-choice support from more than half of all eligible alliances.

To get started with the bot, head over to the [wiki](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki) for instructions and other information.

If you have any issues with the bot, head over to the [common issues](https://github.com/kingshot-project/Kingshot-Discord-Bot/wiki/Getting-Help) page or join our [discord server](https://discord.gg/apYByj6K2m) for support.


### Admin manual ballots

`/vote_manual [fid] [user]` opens a form with an eligible-alliance dropdown and a required reason field, and automatically selects the current
open vote or vote awaiting approval. It opens a private Yes/No/Abstain panel for
standard votes, or the ranking editor with an explicit Abstain action for ranked
votes. Opening or cancelling a panel never records participation. Submit records
an audited manual ballot or correction; finalized votes cannot be changed.

Supply either a player FID or a Discord user, or omit both to use the alliance's
assigned diplomat. An alliance without an assigned diplomat requires explicit
attribution. Unlinked Discord voters are supported; their Discord ID is retained
in the audit reason, separately from the recording administrator. Permissions
and vote status are checked again on submission. Panels expire after 10 minutes.
