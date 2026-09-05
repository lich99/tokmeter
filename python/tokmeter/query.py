"""Bounded aggregation over an immutable snapshot."""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl

BUCKETS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "6h": 21600, "1d": 86400}
TOKEN_COLUMNS = {
    "in": "in_t",
    "cached": "cached_t",
    "out": "out_t",
    "reason": "reason_t",
    "cw5m": "cw5m",
    "cw1h": "cw1h",
    "cr": "cr",
}
COMPONENTS = [
    ("cost_in", "cIn", "Input", "#7C3AED"),
    ("cost_cached", "cCached", "Cached input", "#4D5564"),
    ("cost_out_text", "cOut", "Output (text)", "#FFD81F"),
    ("cost_out_reason", "cReason", "Output (reasoning)", "#FF2BA0"),
    ("cost_cw5m", "cCw5m", "Cache write 5m", "#FF6A1A"),
    ("cost_cw1h", "cCw1h", "Cache write 1h", "#EA4C89"),
    ("cost_cr", "cCr", "Cache read", "#0A0A0A"),
]
MAX_BUCKETS = 5000


def timezone_name(value):
    value = {"ET": "America/New_York", "UTC": "UTC"}.get(value, value)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("tz must be UTC, ET, or an IANA timezone") from None
    return value


def bucket_keys(start, end, granularity, tz):
    """Calendar boundaries, including both folds and excluding nonexistent wall times."""
    zone = ZoneInfo(tz)
    seconds = BUCKETS[granularity]
    local = datetime.fromtimestamp(start / 1000, zone).replace(tzinfo=None)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    floor = midnight + timedelta(seconds=math.floor((local - midnight).total_seconds() / seconds) * seconds)
    until = datetime.fromtimestamp(end / 1000, zone).replace(tzinfo=None) + timedelta(days=1)
    # Select the actual fold containing the start, rather than an earlier repeated hour.
    first = datetime.fromtimestamp(start / 1000, zone)
    lower = int(floor.replace(tzinfo=zone, fold=first.fold).timestamp() * 1000)
    keys = set()
    while floor <= until:
        for fold in (0, 1):
            aware = floor.replace(tzinfo=zone, fold=fold)
            ms = int(aware.timestamp() * 1000)
            if (
                aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == floor
                and lower <= ms < end
            ):
                keys.add(ms)
        floor += timedelta(seconds=seconds)
        if len(keys) > MAX_BUCKETS + 100:
            raise ValueError("Requested range contains too many buckets")
    return sorted(keys)


def aggregate(snapshot, start, end, granularity="1h", tz="ET", fill_gaps=True, source="cc"):
    if not (0 <= start < end <= 4_102_444_800_000):
        raise ValueError("from/to must be increasing UTC milliseconds between 1970 and 2100")
    if source not in ("cc", "codex", "all"):
        raise ValueError("source must be cc, codex, or all")
    g = {"hour": "1h", "day": "1d"}.get(granularity, granularity)
    if g not in BUCKETS:
        raise ValueError("Unsupported granularity")
    requested = g
    tz = timezone_name(tz)
    if (end - start) / 1000 / BUCKETS[g] > MAX_BUCKETS - 2:
        g = next(
            (name for name, seconds in BUCKETS.items() if (end - start) / 1000 / seconds < MAX_BUCKETS - 2),
            "1d",
        )
    if (end - start) / 1000 / BUCKETS[g] > MAX_BUCKETS - 2:
        raise ValueError("Range too large; select a window shorter than 13 years")
    df = snapshot.frame
    if source != "all":
        df = df.filter(pl.col("source") == (0 if source == "cc" else 1))
    sub = df.filter((pl.col("ts") >= start) & (pl.col("ts") < end))
    sums = sub.select(
        *[pl.col(v).sum().alias(k) for k, v in TOKEN_COLUMNS.items()],
        pl.col("cost").sum(),
        (~pl.col("priced")).sum().alias("unpricedCalls"),
        pl.col("tier_assumed").sum().alias("assumedTierCalls"),
    ).row(0, named=True)
    totals = {**sums, "calls": sub.height}
    for key, flag in (("main", 0), ("sub", 1)):
        role = sub.filter(pl.col("is_sub") == flag)
        totals[key] = {"calls": role.height, "cost": role["cost"].sum() or 0.0}

    def breakdown(column, names):
        grouped = (
            sub.group_by(column)
            .agg(
                pl.len().alias("calls"),
                pl.col("cost").sum(),
                (~pl.col("priced")).sum().alias("unpricedCalls"),
            )
            .sort("cost", descending=True)
        )
        return [
            dict(
                name=names[row[column]],
                calls=row["calls"],
                cost=row["cost"],
                unpricedCalls=row["unpricedCalls"],
            )
            for row in grouped.iter_rows(named=True)
        ]

    by_project = breakdown("project", snapshot.projects)
    by_model = breakdown("model", snapshot.models)
    cost_split = [
        {"key": key, "label": label, "val": sub[col].sum() or 0.0, "color": color}
        for col, key, label, color in COMPONENTS
    ]
    local = pl.col("ts").cast(pl.Datetime("ms", "UTC")).dt.convert_time_zone(tz)
    grouped = (
        sub.with_columns(local.dt.truncate(g).dt.epoch("ms").alias("bucket"))
        .group_by("bucket")
        .agg(
            pl.len().alias("calls"),
            *[pl.col(col).sum().alias(name) for name, col in TOKEN_COLUMNS.items()],
            pl.col("cost").sum(),
            (~pl.col("priced")).sum().alias("unpricedCalls"),
            pl.col("cost").filter(pl.col("is_sub") == 0).sum().alias("mainCost"),
            pl.col("cost").filter(pl.col("is_sub") == 1).sum().alias("subCost"),
            *[pl.col(col).sum().alias(key) for col, key, _, _ in COMPONENTS],
        )
    )
    rows = {row.pop("bucket"): row for row in grouped.iter_rows(named=True)}
    keys = bucket_keys(start, end, g, tz) if fill_gaps else sorted(rows)
    columns = [
        "calls",
        *TOKEN_COLUMNS,
        "cost",
        "unpricedCalls",
        "mainCost",
        "subCost",
        *[x[1] for x in COMPONENTS],
    ]
    buckets = {"ts": keys, "step": BUCKETS[g] * 1000}
    for name in columns:
        buckets[name] = [rows.get(key, {}).get(name, 0) for key in keys]
    return dict(
        ready=snapshot.ready,
        generation=snapshot.generation,
        totals=totals,
        byProject=by_project,
        byModel=by_model,
        buckets=buckets,
        source=source,
        timezone=tz,
        granularity=g,
        requestedGranularity=requested,
        costSplit=[x for x in cost_split if x["val"] > 0],
        firstRecordMs=int(df["ts"].min()) if df.height else None,
        totalRecords=df.height,
        files=(snapshot.scan or {}).get("scanned", 0),
        lastRefresh=snapshot.scanned_at,
        scan=snapshot.scan,
        error=snapshot.error,
    )
