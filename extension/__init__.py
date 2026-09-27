"""Kingshot self-hosted extension: verification, NAP, and calendar tooling.

Loaded once by ``main.py`` via ``await bot.load_extension("extension")``. This
package is fully self-contained and optional: nothing in the upstream ``cogs/``
package imports from here, and everything the fork adds lives under this tree.

The ``setup`` entrypoint wires the pieces in order:

    config -> providers -> settings -> storage -> cogs -> tasks
"""

import asyncio
import logging
import os

from . import config

logger = logging.getLogger("extension")


async def setup(bot):
    """Entrypoint invoked by main.py once the upstream cogs are loaded.

    Wires the pieces in order: config -> settings -> schema -> (providers, cogs,
    tasks in later milestones). Everything is attached to the bot so each cog
    reads a single shared configuration without re-parsing the environment.
    """
    cfg = config.load_config()

    # Configure the MightPulse provider (key + cache TTLs) for live-data cogs.
    from .providers import configure_mightpulse
    configure_mightpulse(cfg)

    # Runtime settings live in the upstream bot_global_settings table, seeded
    # from the environment on first run. After that the DB is authoritative.
    from .settings import SettingsStore, seed_from_config
    settings = SettingsStore()
    seed_from_config(settings, cfg)

    # Ensure the unified schema exists: NAP tables + the additive columns on
    # upstream users/alliance_list (idempotent; upstream create_tables already
    # ran before us, so this only fills in the extension's columns).
    from .storage import ensure_schema
    ensure_schema()

    bot.extension_config = cfg
    bot.extension_settings = settings

    # Shared lock so the nightly maintenance and the NAP ranking post never run
    # concurrently (mirrors the monolith's ``maintenance_lock``). The post waits
    # for a rebuild to finish, so it always reads the completed snapshot.
    bot.maintenance_lock = asyncio.Lock()

    # Load the identity cogs, overriding upstream /unregister (always) and
    # /register (only when MightPulse is configured).
    from .cogs.identity import IdentityBase, IdentityMightpulse
    bot.tree.remove_command("unregister")
    await bot.add_cog(IdentityBase(bot))
    if cfg.mightpulse_enabled:
        bot.tree.remove_command("register")
        await bot.add_cog(IdentityMightpulse(bot))

    # Load the remaining extension cogs + scheduled tasks.
    from .cogs.settings import Settings
    from .cogs.nap import Nap
    from .cogs.id_channel import IDChannel
    from .cogs.nap_breaking import NapBreaking
    from .tasks.nightly import NightlyTasks
    await bot.add_cog(Settings(bot))
    await bot.add_cog(Nap(bot))
    await bot.add_cog(IDChannel(bot))
    await bot.add_cog(NapBreaking(bot))
    await bot.add_cog(NightlyTasks(bot))

    logger.info(
        "extension loaded (MightPulse %s)",
        "configured" if cfg.mightpulse_enabled else "not configured",
    )
