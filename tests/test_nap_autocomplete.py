"""NAP exclusion hints reflect the action and retain orphaned exclusions."""
import asyncio
from types import SimpleNamespace

import pytest

from extension.cogs import nap
from extension.nap import NapLogic


@pytest.fixture
def context(monkeypatch):
    logic = NapLogic(alliances={
        "11": {"abbr": "ABC", "name": "Alpha"},
        "22": {"abbr": "DEF", "name": "Delta"},
    }, nap_exclusions={"22": {}, "33": {"tag": "OLD"}, "44": {}})
    monkeypatch.setattr(nap, "_nap_context", lambda bot: (logic, {}))
    monkeypatch.setattr(nap.PermissionManager, "is_admin", lambda uid: (True,))
    return nap.Nap(SimpleNamespace()), SimpleNamespace(user=SimpleNamespace(id=1)), logic


@pytest.mark.parametrize("query", ["", "abc", "ALPHA", "11"])
def test_exclude_suggests_nonexcluded_alliances(context, query):
    cog, interaction, _ = context
    choices = asyncio.run(cog.exclude_aid_autocomplete(interaction, query))
    assert [(c.name, c.value) for c in choices] == [("[ABC] Alpha (ID: 11)", 11)]


def test_unexclude_includes_missing_alliance_rows(context):
    cog, interaction, _ = context
    choices = asyncio.run(cog.unexclude_aid_autocomplete(interaction, ""))
    assert [c.value for c in choices] == [22, 33, 44]
    assert choices[1].name == "[OLD] (ID: 33)"
    assert "44" in choices[2].name
    assert asyncio.run(cog.unexclude_aid_autocomplete(interaction, "old"))[0].value == 33
    assert asyncio.run(cog.unexclude_aid_autocomplete(interaction, "abc")) == []


def test_nonadmin_does_not_load_data(context, monkeypatch):
    cog, interaction, _ = context
    monkeypatch.setattr(nap.PermissionManager, "is_admin", lambda uid: (False,))
    monkeypatch.setattr(nap, "_nap_context", lambda bot: pytest.fail("Must not read data"))
    assert asyncio.run(cog.exclude_aid_autocomplete(interaction, "")) == []
    assert asyncio.run(cog.unexclude_aid_autocomplete(interaction, "")) == []


def test_choice_limits(context):
    cog, interaction, logic = context
    logic.alliances = {str(aid): {"name": "x" * 200} for aid in range(100, 140)}
    choices = asyncio.run(cog.exclude_aid_autocomplete(interaction, ""))
    assert len(choices) == 25
    assert all(len(c.name) <= 100 and str(c.value) in c.name for c in choices)


@pytest.mark.parametrize("command", ["nap_exclude", "nap_unexclude"])
def test_aid_autocomplete_registered(command):
    params = {p.name: p for p in getattr(nap.Nap, command).parameters}
    assert params["aid"].autocomplete