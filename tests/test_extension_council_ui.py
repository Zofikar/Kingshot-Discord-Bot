"""Discord transport contract: public controls, private ballot feedback."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension import storage
from extension.settings import SettingsStore
from extension.vote_storage import VoteStore


@pytest.fixture
def council(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from extension.cogs.council import Council
    storage.ensure_schema()
    settings = SettingsStore()
    store = VoteStore()
    guild = SimpleNamespace(id=1, chunked=True, members=[], get_member=lambda member_id: None,
                            get_role=lambda role_id: None)
    bot = SimpleNamespace(extension_settings=settings, guilds=[guild],
                          get_guild=lambda guild_id: guild, add_view=lambda *a, **kw: None)
    cog = Council(bot)
    return cog, store, guild


def test_reconcile_populates_cache_before_deciding_departures(council):
    cog, _, guild = council
    cog.mandates.assign(1, 7, 42, 99, "admin")
    guild.chunked = False

    async def chunk():
        guild.members = [SimpleNamespace(id=42)]
        guild.chunked = True

    guild.chunk = AsyncMock(side_effect=chunk)
    asyncio.run(cog.reconcile(guild))
    guild.chunk.assert_awaited_once()
    assert cog.mandates.assignments(1) == {7: 42}
    assert len(cog.mandates.history(1)) == 1


def test_incomplete_chunk_cannot_delete_mandates(council):
    cog, _, guild = council
    cog.mandates.assign(1, 7, 42, 99, "admin")
    guild.chunked = False
    guild.chunk = AsyncMock()
    with pytest.raises(ValueError, match="member cache"):
        asyncio.run(cog.reconcile(guild))
    assert cog.mandates.assignments(1) == {7: 42}
    assert len(cog.mandates.history(1)) == 1


def test_maintenance_waits_for_initial_restoration(council):
    cog, _, _ = council
    cog.bot.wait_until_ready = AsyncMock()
    cog.reconcile = AsyncMock()

    async def scenario():
        task = asyncio.create_task(cog.maintenance.coro(cog))
        try:
            await asyncio.sleep(0)
            cog.reconcile.assert_not_awaited()
            await cog.on_ready()
            await asyncio.wait_for(task, timeout=1)
            assert cog.restored
            assert cog.reconcile.await_count == 2
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_buttons_are_persistent_and_only_ballots_disable_after_deadline(council):
    from extension.cogs.council import VoteView
    cog, store, _ = council
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    view = VoteView(cog, store.get(vote))
    assert view.is_persistent()
    assert [b.label for b in view.children] == ["Yes", "No", "Abstain", "Participation"]
    closed = VoteView(cog, store.get(vote), closed=True)
    assert [b.disabled for b in closed.children] == [True, True, True, False]


def test_participation_response_is_ephemeral(council):
    cog, store, _ = council
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    store.cast(vote, 7, "ABSTAINED", 100, 42, now=3)
    interaction = SimpleNamespace(guild_id=1, response=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(cog.show_participation(interaction, vote))
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "BBQ" in kwargs["embed"].to_dict()["fields"][2]["value"]


def test_cast_confirmation_is_private_and_records_represented_not_current_alliance(council):
    cog, store, guild = council
    storage.register_player(fid=100, discord_id=42, alliance_aid=8, rank=3, nickname="Alice")
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    cog.mandates.assign(1, 7, 42, 99, "admin")
    # Reconciliation is independently tested; isolate ballot transport.
    cog.reconcile = AsyncMock()
    user = SimpleNamespace(id=42, mention="<@42>")
    interaction = SimpleNamespace(guild_id=1, guild=guild, user=user,
                                 response=SimpleNamespace(defer=AsyncMock()),
                                 followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(cog.cast_ballot(interaction, vote, "YES"))
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    sent = interaction.followup.send.await_args
    assert sent.kwargs["ephemeral"] is True
    assert "BBQ" in sent.args[0] and "YES" in sent.args[0] and "UTC" in sent.args[0]
    assert store.participation(vote)["YES"] == ["BBQ"]


def test_result_includes_abstentions_submitter_and_manual_audit(council):
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.set_identity(vote, "BBQ", "Alice", 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    store.cast(vote, 7, "YES", 100, 99, now=3, manual=True, reason="Leader voted in game")
    store.conclude(vote, 99, now=4)
    text = asyncio.run(cog.result_text(store.get(vote), guild))
    assert "[BBQ] Alice (100)" in text
    assert "1 voted yes - BBQ" in text
    assert "0 abstained" in text
    assert "Leader voted in game" in text and "<@99>" in text
    assert "UTC" in text


def test_vote_embed_has_absolute_and_relative_deadline(council):
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.set_identity(vote, "BBQ", "Alice", 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    embed = asyncio.run(cog.vote_embed(store.get(vote), guild))
    assert "<t:86402:F>" in embed.description
    assert "<t:86402:R>" in embed.description


def test_unverified_explicitly_appointed_diplomat_can_vote(council):
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    cog.mandates.assign(1, 7, 55, 99, "admin")
    cog.reconcile = AsyncMock()
    interaction = SimpleNamespace(guild_id=1, guild=guild,
        user=SimpleNamespace(id=55, mention="<@55>"),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(cog.cast_ballot(interaction, vote, "YES"))
    assert store.participation(vote)["YES"] == ["BBQ"]
    assert store.audit(vote)[0]["actor"] == 55


def test_manual_ballot_rejects_cross_guild_and_nonadmin(council, monkeypatch):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (False, None))
    interaction = SimpleNamespace(guild_id=1, guild=guild, user=SimpleNamespace(id=55),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(cog.vote_manual.callback(cog, interaction, vote, 7, "YES", "100", "In game"))
    assert store.audit(vote) == []
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True, None))
    interaction.guild_id = 2
    asyncio.run(cog.vote_manual.callback(cog, interaction, vote, 7, "YES", "100", "In game"))
    assert store.audit(vote) == []


def test_publication_failure_does_not_advance_queue(council):
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    store.cast(vote, 7, "YES", 100, 42, now=3)
    store.conclude(vote, 99, now=4)
    store.submit(1, "Next", 8, 200, 43)
    cog.channel = AsyncMock(side_effect=ValueError("Channel unavailable"))
    cog.start_next = AsyncMock()
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(cog.publish_and_advance(store.get(vote), guild))
    cog.start_next.assert_not_awaited()
    assert not store.get(vote)["advanced"]


def test_publication_retry_skips_already_published_result(council):
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    store.cast(vote, 7, "YES", 100, 42, now=3)
    store.conclude(vote, 99, now=4)
    store.update_delivery(vote, result_message_id=987)
    store.submit(1, "Next", 8, 200, 43)
    cog.channel = AsyncMock()
    cog.refresh_message = AsyncMock()
    cog.reconcile = AsyncMock()
    cog.start_next = AsyncMock()
    asyncio.run(cog.publish_and_advance(store.get(vote), guild))
    cog.channel.assert_not_awaited()
    cog.start_next.assert_awaited_once_with(guild)
    assert store.get(vote)["advanced"] == 1