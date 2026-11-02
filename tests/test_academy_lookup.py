"""Kingdom-scoped academy lookup without live API or database access."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from extension.cogs import nap
from extension.nap import NapLogic


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setattr(nap.storage, "alliance_row", lambda aid: {"kid": 245})
    monkeypatch.setattr(nap.storage, "all_alliances", lambda: [])
    fetch = AsyncMock(return_value={"aid": 20, "abbr": "ABC", "name": "Academy"})
    save = Mock()
    monkeypatch.setattr(nap.mightpulse, "fetch_alliance_data", fetch)
    monkeypatch.setattr(nap.storage, "upsert_alliance", save)
    return NapLogic(), fetch, save


def test_fetch_uses_main_kingdom_and_persists_tag(lookup):
    logic, fetch, save = lookup
    assert asyncio.run(nap._resolve_academy_tag(logic, 10, " ABC ")) == 20
    fetch.assert_awaited_once_with(245, "ABC", bypass_local_cache=True)
    assert save.call_args.kwargs["kid"] == 245
    assert logic.set_alliance_academy(10, 20)
    assert logic.academy_tags["20"] == "ABC"


def test_local_lookup_ignores_other_kingdom_and_aliases(lookup, monkeypatch):
    logic, fetch, save = lookup
    logic.nap_tag_aliases["ABC"] = 99
    monkeypatch.setattr(nap.storage, "all_alliances", lambda: [
        {"alliance_id": 99, "kid": 999, "abbr": "ABC"},
        {"alliance_id": 20, "kid": 245, "abbr": "ABC"},
    ])
    assert asyncio.run(nap._resolve_academy_tag(logic, 10, "abc")) == 20
    fetch.assert_not_awaited()
    save.assert_not_called()


@pytest.mark.parametrize("response", [None, {}, {"aid": 20, "kid": 999}])
def test_failed_or_wrong_kingdom_response_is_not_saved(lookup, response):
    logic, fetch, save = lookup
    fetch.return_value = response
    result = asyncio.run(nap._resolve_academy_tag(logic, 10, "ABC"))
    assert isinstance(result, str)
    assert "245" in result
    save.assert_not_called()
    assert not logic.alliances


def test_unknown_kingdom_does_not_guess(lookup, monkeypatch):
    logic, fetch, save = lookup
    monkeypatch.setattr(nap.storage, "alliance_row", lambda aid: None)
    assert "kingdom is unknown" in asyncio.run(nap._resolve_academy_tag(logic, 10, "ABC"))
    fetch.assert_not_awaited()


def test_blank_tag_is_not_removal(lookup):
    logic, fetch, save = lookup
    assert "cannot be blank" in asyncio.run(nap._resolve_academy_tag(logic, 10, "  "))
    fetch.assert_not_awaited()


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("tag", ["ABC", None])
def test_commands_share_lookup_and_preserve_removal(monkeypatch, admin, tag):
    logic = NapLogic(alliances={"10": {"abbr": "MAIN"}}, academies={"10": 30})
    resolver = AsyncMock(return_value=20)
    save = Mock()
    monkeypatch.setattr(nap, "_resolve_academy_tag", resolver)
    monkeypatch.setattr(nap, "_nap_context", lambda bot: (logic, {}))
    monkeypatch.setattr(nap, "_save_nap", save)
    monkeypatch.setattr(nap.storage, "main_fid_for_discord", lambda uid: 123)
    monkeypatch.setattr(nap.storage, "user_row", lambda fid: {"alliance": 10})
    monkeypatch.setattr(nap.PermissionManager, "is_admin", lambda uid: (True, None))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1), response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))
    cog = nap.Nap(SimpleNamespace())
    if admin:
        asyncio.run(nap.Nap.admin_academy_set.callback(cog, interaction, "MAIN", tag))
    else:
        asyncio.run(nap.Nap.academy_set.callback(cog, interaction, tag))
    if tag is None:
        resolver.assert_not_awaited()
        assert "10" not in logic.academies
    else:
        resolver.assert_awaited_once_with(logic, 10, "ABC")
        assert logic.academies["10"] == 20
    save.assert_called_once()