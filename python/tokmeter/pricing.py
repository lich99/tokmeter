"""Explicit, replaceable price data. Unknown models never fall through to old families."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import polars as pl

FIELDS = ("input", "cached", "output", "cw5m", "cw1h", "cr")
COST_COLUMNS = (
    "cost_in",
    "cost_cached",
    "cost_out_text",
    "cost_out_reason",
    "cost_cw5m",
    "cost_cw1h",
    "cost_cr",
)


class Catalog:
    def __init__(self, override: Path | None = None):
        data = json.loads(Path(__file__).with_name("prices.json").read_text())
        if override:
            custom = json.loads(override.read_text())
            data["models"].update(custom.get("models", {}))
            data["aliases"].update(custom.get("aliases", {}))
            data["version"] += "+custom"
        self.version, self.models, self.aliases = data["version"], data["models"], data["aliases"]

        def valid(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0

        for model, rate in self.models.items():
            if not isinstance(rate, dict) or not {"input", "output"} <= rate.keys():
                raise ValueError(f"Model {model} requires input and output rates")
            for key in (*FIELDS, "fast_multiplier", "priority_multiplier", "flex_multiplier"):
                if not valid(rate.get(key, 0)):
                    raise ValueError(f"Invalid {key} price for {model}")
            if "long" in rate:
                long = rate["long"]
                if (
                    not isinstance(long, dict)
                    or type(long.get("threshold")) is not int
                    or not 0 < long["threshold"] < 2**62
                ):
                    raise ValueError(f"Invalid long context threshold for {model}")
                if any(not valid(long.get(key, 0)) for key in FIELDS):
                    raise ValueError(f"Invalid long context price for {model}")
        for alias, target in self.aliases.items():
            if target not in self.models:
                raise ValueError(f"Alias {alias} must point directly to a known model")

    def lookup(self, model):
        return self.models.get(self.aliases.get(model, model))

    def apply(self, frame: pl.DataFrame, models: list[str]) -> pl.DataFrame:
        if not frame.height:
            return frame.with_columns(
                *[pl.lit(0.0, dtype=pl.Float64).alias(c) for c in (*COST_COLUMNS, "cost")],
                pl.lit(False).alias("priced"),
                pl.lit(False).alias("tier_assumed"),
                pl.lit(False).alias("long_ctx"),
            )
        rates = [self.lookup(model) or {} for model in models]
        indices = frame["model"].to_numpy()
        inp, cached, out, reason = (frame[c].to_numpy() for c in ("in_t", "cached_t", "out_t", "reason_t"))
        source, tiers = frame["source"].to_numpy(), frame["tier"].to_numpy()
        known = np.array([bool(r) for r in rates])[indices]
        thresholds = np.array([r.get("long", {}).get("threshold", 2**62) for r in rates], dtype=np.int64)[
            indices
        ]
        # Claude's input field excludes cache reads/writes.
        context = inp + np.where(
            source == 0, frame["cw5m"].to_numpy() + frame["cw1h"].to_numpy() + frame["cr"].to_numpy(), 0
        )
        long = context > thresholds
        mult, supported = np.ones(frame.height), tiers == 1
        for code, key in ((2, "fast_multiplier"), (3, "priority_multiplier"), (4, "flex_multiplier")):
            values = np.array([r.get(key, np.nan) for r in rates])[indices]
            use = (tiers == code) & np.isfinite(values)
            mult[use] = values[use]
            supported |= use

        def rate(key):
            normal = np.array([r.get(key, 0) for r in rates], dtype=float)[indices]
            extended = np.array([r.get("long", {}).get(key, r.get(key, 0)) for r in rates], dtype=float)[
                indices
            ]
            return np.where(long, extended, normal) * mult / 1_000_000

        output_rate = rate("output")
        costs = [
            pl.Series("cost_in", np.maximum(0, inp - cached) * rate("input")),
            pl.Series("cost_cached", cached * rate("cached")),
            pl.Series("cost_out_text", np.maximum(0, out - reason) * output_rate),
            pl.Series("cost_out_reason", reason * output_rate),
        ]
        for token, price in (("cw5m", "cw5m"), ("cw1h", "cw1h"), ("cr", "cr")):
            costs.append(pl.Series("cost_" + token, frame[token].to_numpy() * rate(price)))
        return frame.with_columns(
            *costs,
            pl.Series("priced", known),
            pl.Series("tier_assumed", known & ~supported),
            pl.Series("long_ctx", long),
        ).with_columns(pl.sum_horizontal(*COST_COLUMNS).alias("cost"))
