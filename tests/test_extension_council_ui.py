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


def report_interaction(guild):
    return SimpleNamespace(guild_id=guild.id, guild=guild, user=SimpleNamespace(id=99),
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


def test_manual_vote_id_hints(council, monkeypatch):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (True,))
    vote = store.submit(1, "Council question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}, {"aid": 82, "abbr": "ALT"}])
    store.submit(1, "Queued question", 7, 100, 42)
    store.submit(2, "Other server", 7, 100, 42)
    interaction = report_interaction(guild)
    interaction.namespace = SimpleNamespace(vote_id=vote)
    choices = asyncio.run(cog.manual_alliance_autocomplete(interaction, "bbq"))
    assert [(choice.name, choice.value) for choice in choices] == [("BBQ (ID: 7)", 7)]
    assert asyncio.run(cog.manual_alliance_autocomplete(interaction, "missing")) == []
    parameters = {parameter.name: parameter for parameter in cog.vote_manual.parameters}
    assert set(parameters) == {"fid", "user"}
    interaction.guild_id = 2
    assert asyncio.run(cog.manual_alliance_autocomplete(interaction, "")) == []
    interaction.namespace.vote_id = None
    assert asyncio.run(cog.manual_alliance_autocomplete(interaction, "")) == []
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (False,))
    assert asyncio.run(cog.manual_alliance_autocomplete(interaction, "")) == []
    assert asyncio.run(cog.manual_alliance_autocomplete(interaction, "")) == []


def database_snapshot(store):
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        return list(conn.iterdump())


def test_diplomat_list_is_private_and_read_only(council):
    cog, store, guild = council
    vote = store.submit(1, "Existing vote", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}, {"aid": 8, "abbr": "ALT"}], now=2)
    cog.mandates.assign(1, 7, 42, 99, "admin")
    cog.mandates.assign(2, 8, 55, 99, "admin")
    before = database_snapshot(store)
    interaction = report_interaction(guild)
    asyncio.run(cog.diplomat_list.callback(cog, interaction))
    sent = interaction.followup.send.await_args
    assert "BBQ (7) — <@42>" in sent.args[0]
    assert "ALT (8) — Vacant" in sent.args[0]
    assert "55" not in sent.args[0]
    assert sent.kwargs["ephemeral"]
    assert database_snapshot(store) == before
    assert store.get(vote)["status"] == "OPEN"


@pytest.mark.parametrize("finalized", [False, True])
def test_vote_audit_preserves_existing_votes(council, monkeypatch, finalized):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (True,))
    vote = store.submit(1, "Existing vote", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}, {"aid": 8, "abbr": "ALT"}], now=2)
    store.cast(vote, 7, "NO", 0, 42, now=3)
    store.cast(vote, 7, "YES", 123, 99, now=4, manual=True, reason="Confirmed in game")
    if finalized:
        store.conclude(vote, 99, now=90000)
    before = database_snapshot(store)
    interaction = report_interaction(guild)
    asyncio.run(cog.vote_audit.callback(cog, interaction, vote))
    sent = interaction.followup.send.await_args
    text = sent.args[0]
    assert "Voter <@42>" in text
    assert "FID: none linked" in text
    assert "Entered by admin <@99>" in text
    assert "FID: 123" in text
    assert "replaces NO" in text
    assert "Confirmed in game" in text
    assert "ALT (8) — DID_NOT_VOTE" in text
    assert sent.kwargs["ephemeral"]
    assert database_snapshot(store) == before


@pytest.mark.parametrize("admin,guild_id,error", [
    (False, 1, "admin permission"), (True, 2, "another server"),
])
def test_vote_audit_rejects_unauthorized_access(council, monkeypatch, admin, guild_id, error):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (admin,))
    vote = store.submit(guild_id, "Secret question", 7, 100, 42)
    interaction = report_interaction(guild)
    asyncio.run(cog.vote_audit.callback(cog, interaction, vote))
    text = interaction.followup.send.await_args.args[0]
    assert error in text
    assert "Secret question" not in text


def test_vote_audit_attaches_complete_long_history(council, monkeypatch):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (True,))
    vote = store.submit(1, "Question", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    for index in range(25):
        store.cast(vote, 7, "YES", 100, 42, now=3 + index)
    before = database_snapshot(store)
    interaction = report_interaction(guild)
    asyncio.run(cog.vote_audit.callback(cog, interaction, vote))
    options = interaction.followup.send.await_args.kwargs
    text = options["file"].fp.getvalue().decode("utf-8")
    assert "#1 BBQ" in text and "#25 BBQ" in text
    assert options["ephemeral"]
    assert database_snapshot(store) == before


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


@pytest.mark.parametrize("role,override,expected", [
    ("leader", None, "leader"),
    ("diplomat", None, "handover"),
    ("unverified_diplomat", None, "handover"),
    ("admin", 7, "admin"),
    ("admin", None, None),
    ("leader", 7, None),
    ("outsider", None, None),
    ("wrong_recipient", None, None),
])
def test_diplomat_assign_infers_alliance(council, monkeypatch, role, override, expected):
    from extension.cogs.council import PermissionManager
    cog, _, guild = council
    cog.reconcile = AsyncMock()
    cog.electorate = lambda guild_id: [{"aid": 7}]
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (role == "admin",))
    if role in ("diplomat", "unverified_diplomat", "wrong_recipient"):
        cog.mandates.assign(1, 7, 42, 99, "admin")

    def player(member_id):
        if role in ("admin", "unverified_diplomat") and member_id == 42:
            raise ValueError("A verified MAIN FID is required.")
        return {"rank": 5 if role == "leader" else 3,
                "alliance": 7 if role == "leader" or (member_id == 43 and role != "wrong_recipient") else 8}

    cog.main_player = player
    interaction = SimpleNamespace(guild_id=1, guild=guild, user=SimpleNamespace(id=42),
                                 response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                                 followup=SimpleNamespace(send=AsyncMock()))
    member = SimpleNamespace(id=43, mention="<@43>")
    asyncio.run(cog.diplomat_assign.callback(cog, interaction, member, override))
    if expected:
        assert cog.mandates.assignments(1) == {7: 43}
        assert cog.mandates.history(1)[-1]["action"] == expected
    else:
        assert 43 not in cog.mandates.assignments(1).values()
        assert "now represents" not in interaction.followup.send.await_args.args[0]


def test_diplomat_assign_alliance_option_is_optional():
    from extension.cogs.council import Council
    options = {option.name: option for option in Council.diplomat_assign.parameters}
    assert options["member"].required
    assert not options["alliance_id"].required
    assert options["alliance_id"].autocomplete


@pytest.mark.parametrize("query,expected", [("", [7, 82]), ("mayhem", [7]),
                                          ("bbq", [7]), ("82", [82]), ("missing", [])])
def test_diplomat_alliance_autocomplete_search(council, monkeypatch, query, expected):
    from extension.cogs.council import PermissionManager
    cog, _, _ = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (True,))
    storage.upsert_alliance(7, name="MAYHEM", abbr="BBQ")
    storage.upsert_alliance(99, name="Ineligible", abbr="NO")
    cog.electorate = lambda guild_id: [{"aid": 7, "abbr": "BBQ"}, {"aid": 82, "abbr": "ALT"}]
    interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=42))
    choices = asyncio.run(cog.diplomat_alliance_autocomplete(interaction, query))
    assert [choice.value for choice in choices] == expected
    if 7 in expected:
        assert choices[0].name == "BBQ · MAYHEM (ID: 7)"


def test_diplomat_alliance_autocomplete_limits(council, monkeypatch):
    from extension.cogs.council import PermissionManager
    cog, _, _ = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (True,))
    cog.electorate = lambda guild_id: [{"aid": aid, "abbr": "X" * 120} for aid in range(30)]
    interaction = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=42))
    choices = asyncio.run(cog.diplomat_alliance_autocomplete(interaction, ""))
    assert len(choices) == 25
    assert all(len(choice.name) <= 100 for choice in choices)
    assert choices[0].name.endswith(" (ID: 0)")


@pytest.mark.parametrize("guild_id,admin", [(None, True), (1, False)])
def test_diplomat_alliance_autocomplete_access(council, monkeypatch, guild_id, admin):
    from extension.cogs.council import PermissionManager
    cog, _, _ = council
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member_id: (admin,))
    interaction = SimpleNamespace(guild_id=guild_id, user=SimpleNamespace(id=42))
    assert asyncio.run(cog.diplomat_alliance_autocomplete(interaction, "")) == []


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
                                 response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
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
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
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
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    asyncio.run(cog.open_manual_ballot(interaction, 7, "In game", fid="100"))
    assert store.audit(vote) == []
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True, None))
    interaction.guild_id = 2
    asyncio.run(cog.open_manual_ballot(interaction, 7, "In game", fid="100"))
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


def ranked_vote(store):
    vote = store.submit(1, "NAP", 7, 100, 42, vote_type="RANKED", options=["8", "10", "12"])
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}, {"aid": 8, "abbr": "ALT"}])
    return vote


def test_ranked_controls_and_closed_controls(council):
    from extension.cogs.council import VoteView
    cog, store, guild = council
    vote = ranked_vote(store)
    view = VoteView(cog, store.get(vote))
    assert view.is_persistent()
    assert [b.label for b in view.children] == ["Rank", "Abstain", "Participation"]
    assert [b.disabled for b in VoteView(cog, store.get(vote), closed=True).children] == [True, True, False]


def test_rank_opens_panel_without_recording_participation(council):
    from extension.cogs.council import VoteView, RankingView
    cog, store, guild = council
    vote = ranked_vote(store)
    interaction = report_interaction(guild)
    cog.mandates.assign(1, 7, 99, 99, "admin")
    cog.reconcile = AsyncMock()
    view = VoteView(cog, store.get(vote))
    asyncio.run(view.children[0].callback(interaction))
    sent = interaction.followup.send.await_args
    assert isinstance(sent.kwargs["view"], RankingView)
    assert sent.kwargs["ephemeral"]
    assert store.audit(vote) == []
    assert store.participation(vote)["DID_NOT_VOTE"] == ["BBQ", "ALT"]


def test_ranked_ballot_and_explicit_abstain_callbacks(council):
    from extension.cogs.council import VoteView
    cog, store, guild = council
    vote = ranked_vote(store)
    cog.mandates.assign(1, 7, 99, 99, "admin")
    cog.reconcile = AsyncMock()
    interaction = report_interaction(guild)
    interaction.user.mention = "<@99>"
    asyncio.run(cog.cast_ballot(interaction, vote, [3, 1]))
    assert store.participation(vote)["RANKED"] == ["BBQ"]
    view = VoteView(cog, store.get(vote))
    asyncio.run(view.children[1].callback(interaction))
    assert store.participation(vote)["ABSTAINED"] == ["BBQ"]
    assert store.participation(vote)["RANKED"] == []
    assert interaction.followup.send.await_args.kwargs["ephemeral"]


def test_ranked_manual_input(council, monkeypatch):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    vote = ranked_vote(store)
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True, None))
    interaction = report_interaction(guild)
    async def exercise():
        await cog.open_manual_ballot(interaction, 7, "Confirmed in game", fid="100")
        view = interaction.followup.send.await_args.kwargs["view"]
        assert store.audit(vote) == []
        assert interaction.followup.send.await_args.kwargs["ephemeral"]
        view.ranking = [3, 1]
        interaction.edit_original_response = AsyncMock()
        await view.submit.callback(interaction)
        assert store.audit(vote)[0]["answer"] == "[3, 1]"
        await cog.open_manual_ballot(interaction, 7, "Correction", fid="100")
        view = interaction.followup.send.await_args.kwargs["view"]
        await view.abstain.callback(interaction)
        assert store.participation(vote)["ABSTAINED"] == ["BBQ"]
    asyncio.run(exercise())


def test_ranked_embed_and_result_rounds(council):
    cog, store, guild = council
    vote = ranked_vote(store)
    embed = asyncio.run(cog.vote_embed(store.get(vote), guild))
    assert "1. 8" in embed.description and "3. 12" in embed.description
    assert "Unranked options" in embed.description
    store.cast(vote, 7, [2, 1, 3], 100, 42)
    store.cast(vote, 8, [2, 3, 1], 200, 43)
    store.conclude(vote, 99)
    text = asyncio.run(cog.result_text(store.get(vote), guild))
    assert "Winner: **10**" in text
    assert "Round 1" in text and "2 ranked" in text


def test_ranked_panel_rejects_empty_input(council):
    from extension.cogs.council import RankingView
    cog, store, guild = council
    vote = ranked_vote(store)
    view = RankingView(cog, store.get(vote), 99, 7)
    interaction = report_interaction(guild)
    cog.mandates.assign(1, 7, 99, 99, "admin")
    cog.reconcile = AsyncMock()
    assert view.submit.disabled
    asyncio.run(view.submit.callback(interaction))
    assert interaction.followup.send.await_args.kwargs["ephemeral"]
    assert store.audit(vote) == []


def test_ranking_panel_edit_move_unrank_cancel(council):
    from extension.cogs.council import RankingView
    cog, store, guild = council
    vote = ranked_vote(store)
    store.cast(vote, 7, [2], 100, 42)
    view = RankingView(cog, store.get(vote), 99, 7, [2])
    interaction = report_interaction(guild)
    interaction.response.edit_message = AsyncMock()
    view.selected = 3
    asyncio.run(view.rank.callback(interaction))
    assert view.ranking == [2, 3]
    asyncio.run(view.up.callback(interaction))
    assert view.ranking == [3, 2]
    assert view.up.disabled
    asyncio.run(view.down.callback(interaction))
    assert view.ranking == [2, 3]
    asyncio.run(view.unrank.callback(interaction))
    assert view.ranking == [2]
    assert len(store.audit(vote)) == 1
    asyncio.run(view.cancel.callback(interaction))
    assert all(item.disabled for item in view.children)
    assert len(store.audit(vote)) == 1


def test_ranking_panel_loads_existing_explicit_ballot(council):
    cog, store, guild = council
    vote = ranked_vote(store)
    store.cast(vote, 7, [3, 1], 100, 42)
    cog.mandates.assign(1, 7, 99, 99, "admin")
    cog.reconcile = AsyncMock()
    interaction = report_interaction(guild)
    asyncio.run(cog.open_ranking(interaction, vote))
    view = interaction.followup.send.await_args.kwargs["view"]
    assert view.ranking == [3, 1]
    assert "10" in view.embed().fields[-1].value
    assert len(store.audit(vote)) == 1


def test_ranking_panel_submit_records_and_disables(council):
    from extension.cogs.council import RankingView
    cog, store, guild = council
    vote = ranked_vote(store)
    cog.mandates.assign(1, 7, 99, 99, "admin")
    cog.reconcile = AsyncMock()
    view = RankingView(cog, store.get(vote), 99, 7, [3, 2])
    interaction = report_interaction(guild)
    interaction.user.mention = "<@99>"
    interaction.edit_original_response = AsyncMock()
    asyncio.run(view.submit.callback(interaction))
    assert store.audit(vote)[0]["answer"] == "[3, 2]"
    assert view.finished and all(item.disabled for item in view.children)


def test_ranking_panel_rejects_changed_alliance_and_deadline(council):
    from extension.cogs.council import RankingView
    cog, store, guild = council
    vote = ranked_vote(store)
    cog.mandates.assign(1, 8, 99, 99, "admin")
    cog.reconcile = AsyncMock()
    view = RankingView(cog, store.get(vote), 99, 7, [1])
    interaction = report_interaction(guild)
    asyncio.run(view.submit.callback(interaction))
    assert store.audit(vote) == []
    cog.mandates.assign(1, 7, 55, 99, "admin")
    view = RankingView(cog, store.get(vote), 55, 7, [1])
    interaction.user.id = 55
    store.expire(now=store.get(vote)["closes_at"])
    asyncio.run(view.submit.callback(interaction))
    assert store.audit(vote) == []


def test_ranking_panel_owner_check_and_large_options(council):
    from extension.cogs.council import RankingView
    cog, store, guild = council
    vote = store.submit(1, "Many", 7, 100, 42, vote_type="RANKED",
                        options=[str(i) + "*" * 78 for i in range(20)])
    view = RankingView(cog, store.get(vote), 55, 7)
    interaction = report_interaction(guild)
    interaction.response.send_message = AsyncMock()
    assert not asyncio.run(view.interaction_check(interaction))
    assert len(view.selector.options) == 20
    assert all(len(field.value) <= 1024 for field in view.embed().fields)


def test_ranking_panel_selection_timeout_and_closed_check(council):
    from extension.cogs.council import RankingView
    cog, store, guild = council
    vote = ranked_vote(store)
    view = RankingView(cog, store.get(vote), 99, 7)
    interaction = report_interaction(guild)
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    view.selector._values = ["2"]
    asyncio.run(view.select_answer(interaction))
    assert view.selected == 2
    assert not view.rank.disabled and view.unrank.disabled
    assert view.selector.options[1].default
    assert store.audit(vote) == []
    asyncio.run(view.on_timeout())
    assert all(item.disabled for item in view.children)
    assert not asyncio.run(view.interaction_check(interaction))


def test_open_ranking_rejects_nonrepresentative_and_expired_vote(council):
    cog, store, guild = council
    vote = ranked_vote(store)
    cog.reconcile = AsyncMock()
    interaction = report_interaction(guild)
    asyncio.run(cog.open_ranking(interaction, vote))
    assert "active diplomat" in interaction.followup.send.await_args.args[0]
    store.expire(now=store.get(vote)["closes_at"])
    asyncio.run(cog.open_ranking(interaction, vote))
    assert "closed" in interaction.followup.send.await_args.args[0]
    assert store.audit(vote) == []

@pytest.mark.parametrize("answer_index,answer", [(0, "YES"), (1, "NO"), (2, "ABSTAINED")])
def test_manual_standard_private_panel(council, monkeypatch, answer_index, answer):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    cog.mandates.assign(1, 7, 55, 99, "admin")
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True,))
    monkeypatch.setattr(storage, "main_fid_for_discord", lambda member: None)
    interaction = report_interaction(guild)
    interaction.edit_original_response = AsyncMock()

    async def exercise():
        await cog.open_manual_ballot(interaction, 7, "In game")
        assert store.audit(vote) == []
        view = interaction.followup.send.await_args.kwargs["view"]
        assert interaction.followup.send.await_args.kwargs["ephemeral"]
        await view.children[answer_index].callback(interaction)
        row = store.audit(vote)[0]
        assert row["answer"] == answer
        assert row["actor"] == 99 and row["fid"] == 0 and row["manual"]
        assert "voter Discord ID: 55" in row["reason"]
        await view.children[answer_index].callback(interaction)
        assert len(store.audit(vote)) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["other_actor", "permission", "finalized"])
def test_manual_panel_revalidates_at_submit(council, monkeypatch, failure):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True,))
    interaction = report_interaction(guild)
    async def exercise():
        await cog.open_manual_ballot(interaction, 7, "In game", fid="100")
        view = interaction.followup.send.await_args.kwargs["view"]
        if failure == "other_actor":
            interaction.user.id = 123
        elif failure == "permission":
            monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (False,))
        else:
            store.cast(vote, 7, "YES", 100, 99)
            store.conclude(vote, 99)
        before = len(store.audit(vote))
        await view.children[1].callback(interaction)
        assert len(store.audit(vote)) == before
    asyncio.run(exercise())


@pytest.mark.parametrize("kwargs", [{}, {"fid": "0"}, {"fid": "oops"},
    {"fid": "100", "user": SimpleNamespace(id=55)}])
def test_manual_panel_rejects_invalid_attribution(council, monkeypatch, kwargs):
    from extension.cogs.council import PermissionManager
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True,))
    interaction = report_interaction(guild)
    asyncio.run(cog.open_manual_ballot(interaction, 7, "In game", **kwargs))
    assert "view" not in interaction.followup.send.await_args.kwargs
    assert store.audit(vote) == []


@pytest.mark.parametrize("count", [1, 25, 26, 100])
def test_manual_entry_modal(council, monkeypatch, count):
    from extension.cogs.council import PermissionManager, ManualEntryModal
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": i, "abbr": f"A{i}"} for i in range(1, count + 1)])
    monkeypatch.setattr(PermissionManager, "is_admin", lambda member: (True,))
    interaction = report_interaction(guild)
    interaction.response.send_modal = AsyncMock()
    async def exercise():
        await cog.vote_manual.callback(cog, interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, ManualEntryModal)
        assert [int(o.value) for select in modal.alliances for o in select.options] == list(range(1, count + 1))
        assert modal.reason.required and modal.reason.max_length == 1000
        assert len(modal.to_components()) <= 5
        modal.alliances[0]._values = ["1"]
        modal.reason._value = "In game"
        cog.open_manual_ballot = AsyncMock()
        await modal.on_submit(interaction)
        assert cog.open_manual_ballot.await_args.args[1:3] == (1, "In game")
        assert cog.open_manual_ballot.await_args.kwargs["vote_id"] == vote
        assert store.audit(vote) == []
    asyncio.run(exercise())


def test_manual_entry_modal_rejects_multiple_selections(council):
    from extension.cogs.council import ManualEntryModal
    cog, store, guild = council
    vote = store.submit(1, "Question", 7, 100, 42)
    store.open_next(1, [{"aid": i, "abbr": str(i)} for i in range(26)])
    async def exercise():
        modal = ManualEntryModal(cog, store.get(vote), 99)
        modal.alliances[0]._values = ["1"]
        modal.alliances[1]._values = ["25"]
        cog.open_manual_ballot = AsyncMock()
        await modal.on_submit(report_interaction(guild))
        cog.open_manual_ballot.assert_not_awaited()
    asyncio.run(exercise())
