"""Cost from tokens. Tokens are the recorded fact; money is derived when read, because prices change.

A model's price, per million tokens, is the first of:
1. ZHIQIU_PRICE_MODELS: prices typed in per model name;
2. ZHIQIU_PRICE_INPUT and ZHIQIU_PRICE_OUTPUT (optional ZHIQIU_PRICE_CACHED_INPUT, cache hits; defaults
   to the input price), typed in for the configured model (ZHIQIU_LLM_MODEL);
3. the vendor's price page, read by `zhiqiu price update` (prices.py);
4. the typed-in prices of 2. for any other model, as before 9.2.
Without a price the cost is None and only tokens are shown. A page price may have an off-peak rate:
each call is priced by the time it started. An amount in another currency than the one shown is
left out rather than added to it.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import prices as catalog

# The currencies a price page may name, and how amounts in them are shown.
SYMBOLS = {"CNY": "¥", "USD": "$"}


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cached_input: float
    # Shown before amounts (¥, $); "" when nobody said.
    currency: str = ""
    # "手填" or "价格页".
    source: str = "手填"
    # From a price page: the off-peak rates, the peak hours they do not apply in, and when the page was read.
    off_peak: dict[str, float] | None = None
    peak_hours: tuple[dict[str, Any], ...] = ()
    utc_offset_minutes: int = 0
    fetched_at: str = ""
    window: int | None = None

    def peak(self, at_ns: int | None = None) -> bool:
        """Whether a call started at at_ns (now when None) falls in the page's peak hours."""
        if not self.peak_hours:
            return True
        moment = datetime.fromtimestamp((at_ns if at_ns is not None else time.time_ns()) / 1e9, timezone.utc)
        local = moment + timedelta(minutes=self.utc_offset_minutes)
        clock = local.strftime("%H:%M")
        return any(local.isoweekday() in span["days"] and span["start"] <= clock < span["end"]
                   for span in self.peak_hours)

    def rates(self, at_ns: int | None = None) -> dict[str, float]:
        if self.off_peak and self.peak_hours and not self.peak(at_ns):
            return self.off_peak
        return {"input": self.input, "output": self.output, "cached_input": self.cached_input}

    def as_dict(self, at_ns: int | None = None) -> dict[str, Any]:
        return {"input": self.input, "output": self.output, "cached_input": self.cached_input,
                "currency": self.currency, "source": self.source, "off_peak": self.off_peak,
                "peak_now": self.peak(at_ns) if self.off_peak and self.peak_hours else None,
                "now": self.rates(at_ns), "fetched_at": self.fetched_at, "window": self.window}


def _price(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _typed() -> Price | None:
    input_price, output_price = _price("ZHIQIU_PRICE_INPUT"), _price("ZHIQIU_PRICE_OUTPUT")
    if input_price is None or output_price is None:
        return None
    cached = _price("ZHIQIU_PRICE_CACHED_INPUT")
    return Price(input_price, output_price, input_price if cached is None else cached,
                 os.getenv("ZHIQIU_PRICE_CURRENCY", "").strip())


def _typed_models() -> dict[str, Price]:
    """ZHIQIU_PRICE_MODELS: {"模型名": {"input": 2, "output": 8, "cached_input": 0.5, "currency": "¥"}}.
    An entry without both prices is skipped."""
    try:
        table = json.loads(os.getenv("ZHIQIU_PRICE_MODELS", "") or "{}")
    except json.JSONDecodeError:
        return {}
    result = {}
    for name, entry in (table.items() if isinstance(table, dict) else ()):
        if not isinstance(entry, dict):
            continue
        values = {key: entry.get(key) for key in ("input", "output", "cached_input")}
        if not all(type(values[key]) in (int, float) and values[key] >= 0 for key in ("input", "output")):
            continue
        cached = values["cached_input"] if type(values["cached_input"]) in (int, float) else values["input"]
        currency = entry.get("currency") if isinstance(entry.get("currency"), str) else os.getenv("ZHIQIU_PRICE_CURRENCY", "")
        result[str(name).lower()] = Price(float(values["input"]), float(values["output"]), float(cached), currency.strip())
    return result


def default_model() -> str:
    return os.getenv("ZHIQIU_LLM_MODEL", "").strip()


def _from_page(entry: dict[str, Any], fetched_at: str) -> Price:
    return Price(entry["input"], entry["output"], entry["cached_input"], SYMBOLS.get(entry["currency"], ""),
                 "价格页", entry.get("off_peak"), tuple(entry.get("peak_hours") or ()),
                 int(entry.get("utc_offset_minutes") or 0), fetched_at, entry.get("window"))


def price_for(model: str | None = None) -> Price | None:
    name = (model or default_model()).strip()
    typed = _typed()
    by_model = _typed_models()
    if name.lower() in by_model:
        return by_model[name.lower()]
    if typed is not None and (not name or name == default_model()):
        return typed
    entry, fetched_at = catalog.lookup(name)
    if entry is not None:
        return _from_page(entry, fetched_at)
    return typed


def prices() -> dict[str, float] | None:
    """The configured model's prices now, or None (kept for callers from before per-model prices)."""
    price = price_for()
    return price.rates() if price else None


def currency() -> str:
    """The symbol amounts are shown with: typed in, else that of the configured model's price."""
    typed = os.getenv("ZHIQIU_PRICE_CURRENCY", "").strip()
    if typed:
        return typed
    price = price_for()
    return price.currency if price else ""


def cost(tokens_in: int | None, tokens_out: int | None, tokens_cached: int | None = None,
         model: str | None = None, at_ns: int | None = None) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    shown = currency()
    if price.currency and shown and price.currency != shown:
        return None
    table = price.rates(at_ns)
    total_in = tokens_in or 0
    cached = min(tokens_cached or 0, total_in)
    value = ((total_in - cached) * table["input"] + cached * table["cached_input"]
             + (tokens_out or 0) * table["output"]) / 1_000_000
    return round(value, 6)


def calls_cost(rows: Iterable[Any]) -> float | None:
    """The cost of model calls (rows with model, started_at_ns and the token columns), each at its own
    model's price and time; None when not one of them could be priced."""
    total, priced = 0.0, False
    for row in rows:
        value = cost(row["tokens_in"], row["tokens_out"], row["tokens_cached"], row["model"], row["started_at_ns"])
        if value is not None:
            total += value
            priced = True
    return round(total, 6) if priced else None
