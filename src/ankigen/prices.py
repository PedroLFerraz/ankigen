"""What a run's tokens cost, from the price table in prices.yaml.

An estimate of the model bill only: no compute, storage or network, and a
free tier is 0 only while it stays inside its allowance.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PRICES_PATH = Path(__file__).with_name("prices.yaml")


@lru_cache(maxsize=1)
def load_prices(path: Path = PRICES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def price_of(model: str, prices: dict | None = None) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None for a model with no price."""
    models = (load_prices() if prices is None else prices).get("models") or {}
    if model in models:
        return float(models[model]["input"]), float(models[model]["output"])
    if model.endswith(":free"):
        return 0.0, 0.0
    return None


def cost_usd(details: dict, prices: dict | None = None) -> dict:
    """The cost of a run, from each stage's recorded tokens per model.

    A model with no price is listed in `unpriced` and left out of every sum.
    """
    prices = load_prices() if prices is None else prices
    by_stage: dict[str, float] = {}
    by_model: dict[str, float] = {}
    unpriced: set[str] = set()
    for stage, detail in details.items():
        for model, used in (detail.get("tokens") or {}).items():
            rate = price_of(model, prices)
            if rate is None:
                unpriced.add(model)
                continue
            usd = (used.get("prompt", 0) * rate[0] + used.get("completion", 0) * rate[1]) / 1e6
            by_stage[stage] = by_stage.get(stage, 0.0) + usd
            by_model[model] = by_model.get(model, 0.0) + usd
    return {"llm_usd": round(sum(by_model.values()), 6),
            "by_stage": {k: round(v, 6) for k, v in by_stage.items()},
            "by_model": {k: round(v, 6) for k, v in by_model.items()},
            "unpriced": sorted(unpriced),
            "prices_checked": str(prices.get("checked", ""))}
