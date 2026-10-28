"""@brief Cost of a DeepSeek API call from its tokens and the official price list.

DeepSeek's API returns no cost and has no usage or billing endpoint (only
GET /user/balance, whose update delay is not documented). Its billing rule
is public: «The expense = number of tokens × price»
(https://api-docs.deepseek.com/quick_start/pricing). This module applies it,
so every call has its cost at once, and calls recorded without one can be
costed later from their tokens and time (user decision, HANDOFF 128: on the
RS485 project the 7 DeepSeek calls recorded at that moment gave 0.047 USD
against the dashboard's «Total cost $0.05»).

@note The price list lives here, with its source and the date it was read:
    DeepSeek publishes no machine-readable prices. When DeepSeek changes
    them, PRICES and PRICES_CHECKED must be updated; a model not in the list
    gets no cost (never a guessed one).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

#: Where the prices were read, and when. Re-read on 2026-10-27 (review E4):
#: prices, aliases and the peak rule below match the page on that date.
PRICES_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
PRICES_CHECKED = "2026-10-27"

#: USD per 1M tokens, OFF-PEAK: (input cache hit, input cache miss, output).
#: Peak hours cost twice as much (PEAK_FACTOR). Page table of 2026-10-27:
#: deepseek-flash $0.003/$0.006, $0.15/$0.3, $0.6/$1.2 (off-peak/peak);
#: deepseek-v4-pro $0.022/$0.044, $0.66/$1.32, $1.98/$3.96.
PRICES = {
    "deepseek-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}
#: Names DeepSeek still accepts and bills at the price of another model
#: (page of 2026-10-27: «deepseek-v4-flash» and «deepseek-v4-flash-vision-exp»
#: are served by DeepSeek-V4.1-Flash and billed at the Flash price).
ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
#: Published rule, VERIFIED on PRICES_SOURCE on 2026-10-27 (footnote 2 of the
#: page): «Off-peak rates are half of the peak rates», hence peak = 2 x the
#: off-peak prices stored above.
PEAK_FACTOR = 2.0
#: Peak windows, UTC, Monday to Friday: [start hour, end hour). Published
#: text of 2026-10-27: «Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC,
#: Monday through Friday, excluding Chinese public holidays. All other hours
#: are off-peak, including weekends and Chinese public holidays in full.»
#: Chinese public holidays are not modelled (no calendar in the program): on
#: those days the cost computed here is an upper bound, never lower than the
#: bill.
PEAK_HOURS_UTC = ((1, 4), (6, 10))


def price_of(model: str) -> Optional[tuple]:
    """@brief Off-peak prices of a DeepSeek model name.

    @param model Model name as DeepSeek knows it (no «deepseek-api/» prefix).
    @return (cache hit, cache miss, output) USD per 1M tokens, or None.
    """
    name = str(model or "").strip().lower()
    return PRICES.get(ALIASES.get(name, name))


def is_peak(moment: datetime) -> bool:
    """@brief Tell whether a moment falls in DeepSeek's peak price hours.

    @param moment Time of the call; a naive datetime is taken as UTC.
    @return True Monday-Friday inside PEAK_HOURS_UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.weekday() >= 5:
        return False
    return any(start <= moment.hour < end for start, end in PEAK_HOURS_UTC)


def _count(container: Any, key: str) -> Optional[int]:
    value = container.get(key) if isinstance(container, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0 or value != int(value):
            return None   # negative or fractional: a damaged count, not a count
    except OverflowError:   # an int too large for a float: not a real count
        return None
    return int(value)


def call_cost(model: str, usage: Any, moment: Optional[datetime] = None) -> Optional[float]:
    """@brief Cost in USD of one call, as DeepSeek bills it.

    @param model DeepSeek model name.
    @param usage The `usage` object of the response (or of a recorded call:
        prompt_tokens and completion_tokens are enough).
    @param moment Time the request was sent (default: now).
    @return The cost, or None when the model is not in the price list or
        either token count is missing or unusable (negative, fractional,
        non-finite, boolean).
    @note Cache-hit input tokens are read where DeepSeek puts them (at the
        top of `usage` or under `prompt_tokens_details`, the docs show both);
        when the split is missing every input token counts as a cache miss,
        the dearer price: the cost is then an upper bound, never lower than
        the bill.
    """
    prices = price_of(model)
    if prices is None or not isinstance(usage, dict):
        return None
    prompt = _count(usage, "prompt_tokens")
    output = _count(usage, "completion_tokens")
    if prompt is None or output is None:
        # Both counts are needed: a missing or unusable one makes the cost
        # unknown, not partial -- pricing the other alone would understate
        # the bill as a known amount (audit 138, B5).
        return None
    details = usage.get("prompt_tokens_details")
    hit = _count(usage, "prompt_cache_hit_tokens")
    if hit is None:
        hit = _count(details, "prompt_cache_hit_tokens")
    if hit is None:
        hit = _count(details, "cached_tokens")
    hit = min(hit or 0, prompt)
    factor = PEAK_FACTOR if is_peak(moment or datetime.now(timezone.utc)) else 1.0
    hit_price, miss_price, output_price = prices
    cost = (hit * hit_price + (prompt - hit) * miss_price + output * output_price) * factor
    return round(cost / 1_000_000, 8)
