"""Environment + runtime configuration for the extension.

Reads the same environment variables the pre-migration ``main.py`` used, and
exposes them as a single :class:`Config` object.

Runtime settings (the ones that can later be overridden via ``/admin_setting``
and persisted to SQLite) are seeded here from the environment and defaulted so
the extension behaves exactly like the original bot did.
"""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# The upstream bot reads bot_token.txt and never loads .env, so the extension
# owns this. python-dotenv is an upstream dependency already. load_dotenv() does
# not override variables already present in the real environment (container
# env_file etc.), which is what we want.
load_dotenv()


def _int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Config:
    # MightPulse (live player / alliance / kingdom-ranking data provider).
    # Actions that need live game data are gated on this being set.
    mightpulse_api_key: Optional[str] = None

    # Discord role / channel wiring.
    verified_role_id: Optional[int] = None
    role_bound_top: Optional[int] = None
    role_r5_id: Optional[int] = None
    role_r4_id: Optional[int] = None
    role_council_id: Optional[int] = None
    nap_channel_id: Optional[int] = None
    leaders_nap_post_channel: Optional[int] = None
    unified_id_channel_id: Optional[int] = None

    # Kingdom restriction (None = allow all kingdoms).
    allowed_kingdom_id: Optional[int] = None

    # NAP / council tunables.
    council_top_alliances_count: int = 20
    nap_alliances_count: int = 10
    nap_candidate_multiplier: float = 2.0
    nap_snapshot_lookback_protected: int = 0

    # Local cache TTLs (seconds).
    player_cache_ttl_seconds: int = 900
    alliance_cache_ttl_seconds: int = 3600

    @property
    def mightpulse_enabled(self) -> bool:
        """True when live game-data actions can run."""
        return bool(self.mightpulse_api_key)


def load_config() -> Config:
    """Parse the environment into a Config instance."""
    return Config(
        mightpulse_api_key=os.getenv("MIGHTPULSE_API_KEY") or None,
        verified_role_id=_int("VERIFIED_ROLE_ID"),
        role_bound_top=_int("ROLE_BOUND_TOP"),
        role_r5_id=_int("ROLE_R5_ID"),
        role_r4_id=_int("ROLE_R4_ID"),
        role_council_id=_int("ROLE_COUNCIL_ID"),
        nap_channel_id=_int("NAP_CHANNEL_ID"),
        leaders_nap_post_channel=_int("LEADERS_NAP_POST_CHANNEL"),
        unified_id_channel_id=_int("UNIFIED_ID_CHANNEL_ID"),
        allowed_kingdom_id=_int("ALLOWED_KINGDOM_ID"),
        council_top_alliances_count=_int("COUNCIL_TOP_ALLIANCES_COUNT", 20),
        nap_alliances_count=_int("NAP_ALLIANCES_COUNT", 10),
        nap_candidate_multiplier=_float("NAP_CANDIDATE_MULTIPLIER", 2.0),
        nap_snapshot_lookback_protected=max(0, _int("NAP_SNAPSHOT_LOOKBACK_PROTECTED", 0)),
        player_cache_ttl_seconds=_int("PLAYER_CACHE_TTL_SECONDS", 900),
        alliance_cache_ttl_seconds=_int("ALLIANCE_CACHE_TTL_SECONDS", 3600),
    )
