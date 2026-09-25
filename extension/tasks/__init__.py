"""Scheduled tasks (daily alliance maintenance, daily NAP post).

Wrapped in a cog so ``discord.ext.tasks.Loop`` instances are owned by a loaded
cog and guarded by ``mightpulse_enabled`` (skip when no key). Populated in
milestone 5.
"""
