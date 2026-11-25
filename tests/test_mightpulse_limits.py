import asyncio
import time

import pytest

from extension.providers.mightpulse_api import AccountLimits, ApiResponse, MightPulseAPI


@pytest.mark.parametrize("mighty,minute,daily", [(False, 60, 5000), (True, 360, 30000)])
def test_account_usage_updates_tier(mighty, minute, daily):
    limits = AccountLimits()
    limits.usage({"ok": True, "mighty": mighty,
                  "minute": {"limit": minute, "remaining": minute - 2,
                             "resets_at": "2099-01-01T00:00:00Z"},
                  "daily": {"limit": daily, "remaining": daily - 2,
                            "resets_at": "2099-01-02T00:00:00Z"}})
    assert limits.mighty is mighty
    assert limits.capacities == {"minute": minute, "daily": daily}
    assert asyncio.run(limits.acquire(3))
    assert limits.buckets["minute"][0] == minute - 5


def test_clients_share_account_state():
    first = MightPulseAPI("quota-sharing-test")
    second = MightPulseAPI("quota-sharing-test")
    assert first._limits is second._limits
    assert first._limits is not MightPulseAPI("other-quota-test")._limits


def test_exhausted_daily_budget_returns_without_sleeping():
    limits = AccountLimits()
    limits.buckets["daily"] = (0, time.time() + 3600)
    assert asyncio.run(limits.acquire()) is False


def test_headers_update_limits_and_retry_after():
    limits = AccountLimits()
    reset = time.time() + 120
    limits.observe(ApiResponse(429, {
        "X-RateLimit-Limit-Minute": "360",
        "X-RateLimit-Remaining-Minute": "0",
        "X-RateLimit-Reset-Minute": str(reset),
        "Retry-After": "120",
    }, {}))
    assert limits.capacities["minute"] == 360
    assert limits.buckets["minute"] == (0, reset)
    assert asyncio.run(limits.acquire()) is False