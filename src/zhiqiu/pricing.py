"""Cost from tokens. Tokens are the recorded fact; money is derived when read, because prices change.

Prices are per million tokens, from ZHIQIU_PRICE_INPUT, ZHIQIU_PRICE_OUTPUT and the optional
ZHIQIU_PRICE_CACHED_INPUT (cache hits; defaults to the input price). Without input and output
prices every cost is None and only tokens are shown.
"""
from __future__ import annotations

import os


def _price(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def prices() -> dict[str, float] | None:
    input_price, output_price = _price("ZHIQIU_PRICE_INPUT"), _price("ZHIQIU_PRICE_OUTPUT")
    if input_price is None or output_price is None:
        return None
    cached = _price("ZHIQIU_PRICE_CACHED_INPUT")
    return {"input": input_price, "output": output_price, "cached_input": input_price if cached is None else cached}


def currency() -> str:
    return os.getenv("ZHIQIU_PRICE_CURRENCY", "").strip()


def cost(tokens_in: int | None, tokens_out: int | None, tokens_cached: int | None = None) -> float | None:
    table = prices()
    if table is None:
        return None
    total_in = tokens_in or 0
    cached = min(tokens_cached or 0, total_in)
    value = ((total_in - cached) * table["input"] + cached * table["cached_input"]
             + (tokens_out or 0) * table["output"]) / 1_000_000
    return round(value, 6)
