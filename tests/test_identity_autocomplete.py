"""Account hints stay owner-scoped and preserve multi-account input."""
import asyncio
from types import SimpleNamespace

import pytest

from extension.cogs import identity


@pytest.fixture
def accounts(monkeypatch):
    monkeypatch.setattr(identity.storage, "fids_for_discord",
                        lambda owner: [(101, True), (202, False)] if owner == 42 else [])
    monkeypatch.setattr(identity.storage, "user_row",
                        lambda fid: {"nickname": "Alpha" if fid == 101 else "Beta"})


def test_single_account_search_by_name_and_id(accounts):
    choices = asyncio.run(identity._account_choices(42, "ALP"))
    assert [(c.name, c.value) for c in choices] == [("Alpha (101) · main", 101)]
    assert asyncio.run(identity._account_choices(42, "202"))[0].value == 202
    assert asyncio.run(identity._account_choices(99, "")) == []


@pytest.mark.parametrize("current", ["101, Be", "101 Be", "101, "])
def test_multiple_accounts_preserve_prefix_and_skip_selected(accounts, current):
    choices = asyncio.run(identity._account_choices(42, current, multiple=True))
    assert [c.value for c in choices] == ["101, 202"]


def test_invalid_list_prefix_returns_no_hints(accounts):
    assert asyncio.run(identity._account_choices(42, "oops, ", multiple=True)) == []


def test_limits_and_missing_names(monkeypatch):
    monkeypatch.setattr(identity.storage, "fids_for_discord",
                        lambda owner: [(fid, False) for fid in range(1, 40)])
    monkeypatch.setattr(identity.storage, "user_row", lambda fid: {"nickname": "x" * 200})
    choices = asyncio.run(identity._account_choices(42, ""))
    assert len(choices) == 25
    assert all(len(c.name) <= 100 for c in choices)
    monkeypatch.setattr(identity.storage, "user_row", lambda fid: None)
    assert asyncio.run(identity._account_choices(42, ""))[0].name == "Unnamed (1)"


def test_admin_hints_require_permission_and_selected_member(accounts, monkeypatch):
    interaction = SimpleNamespace(user=SimpleNamespace(id=9), namespace=SimpleNamespace())
    monkeypatch.setattr(identity.PermissionManager, "is_admin", lambda uid: (False,))
    interaction.namespace.member = SimpleNamespace(id=42)
    assert asyncio.run(identity._member_account_autocomplete(interaction, "")) == []
    monkeypatch.setattr(identity.PermissionManager, "is_admin", lambda uid: (True,))
    assert len(asyncio.run(identity._member_account_autocomplete(interaction, ""))) == 2
    assert asyncio.run(identity._source_accounts_autocomplete(interaction, "")) == []
    interaction.namespace.from_user = SimpleNamespace(id=42)
    assert asyncio.run(identity._source_accounts_autocomplete(interaction, "101, "))[0].value == "101, 202"


@pytest.mark.parametrize("command,parameter", [
    ("unregister", "fids"), ("transfer", "fids"), ("set_main_fid", "fid"),
    ("admin_transfer", "fids"), ("admin_set_main", "fid"),
])
def test_commands_have_autocomplete_registered(command, parameter):
    params = {p.name: p for p in getattr(identity.IdentityBase, command).parameters}
    assert params[parameter].autocomplete