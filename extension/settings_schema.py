"""Registry and validation for editable extension runtime settings."""
import json
import math

CATEGORIES = {
    "Identity": ("verified_role_id", "role_bound_top", "role_r5_id", "role_r4_id",
                 "unified_id_channel_id", "whitelist_kids", "whitelist_alliances"),
    "Council & Voting": ("role_council_id", "role_diplomat_id", "vote_channel_id",
                         "vote_result_announcement_channel",
                         "vote_approval_notification_channel_id",
                         "vote_approval_notification_role_id"),
    "NAP": ("nap_channel_id", "leaders_nap_post_channel", "nap_alliances_count",
            "nap_candidate_multiplier", "nap_snapshot_lookback_protected"),
}
SETTING_NAMES = tuple(name for names in CATEGORIES.values() for name in names)


def validate_setting(name: str, value: str) -> str:
    """Canonicalize a known setting or raise ValueError without writing."""
    if name not in SETTING_NAMES:
        raise ValueError("Unknown setting. Open /settings → Extension Settings.")
    value = value.strip()
    if name.startswith("whitelist_"):
        try:
            values = json.loads(value or "[]")
        except ValueError:
            raise ValueError("Enter a JSON list of positive integer IDs, e.g. [123, 456].") from None
        if not isinstance(values, list) or any(type(item) is not int or item <= 0 for item in values):
            raise ValueError("Enter a JSON list of positive integer IDs.")
        return json.dumps(list(dict.fromkeys(values)))
    if name == "nap_candidate_multiplier":
        try:
            number = float(value)
        except ValueError:
            raise ValueError("Enter a finite number of at least 1.") from None
        if not math.isfinite(number) or number < 1:
            raise ValueError("Enter a finite number of at least 1.")
        return str(number)
    optional = name.endswith("_id") or name in (
        "vote_result_announcement_channel", "role_bound_top", "leaders_nap_post_channel")
    if optional and not value:
        return ""
    try:
        number = int(value)
    except ValueError:
        raise ValueError("Enter an integer" + (", or blank to disable." if optional else ".")) from None
    minimum = 0 if name == "nap_snapshot_lookback_protected" else 1
    if number < minimum:
        raise ValueError(f"Value must be at least {minimum}.")
    if optional and number >= 2 ** 64:
        raise ValueError("Discord IDs must fit in an unsigned 64-bit integer.")
    return str(number)