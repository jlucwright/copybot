#!/usr/bin/env python3
"""Summarise causal paper observations without inventing a promotion threshold."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import median
from typing import Any


def metric_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": round(min(values), 8) if values else None,
        "median": round(median(values), 8) if values else None,
        "max": round(max(values), 8) if values else None,
    }


def summarise(events: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(str(row.get("kind")) for row in events)
    quotes = [row for row in events if row.get("kind") == "follower_quote"]
    lane_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    category_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in quotes:
        lane_rows[str(row["lane"])].append(row)
        categories = row.get("market_categories") or ["UNCLASSIFIED"]
        for category in categories:
            category_rows[str(category)].append(row)

    feed_detections: dict[str, dict[str, int]] = defaultdict(dict)
    for row in events:
        if row.get("kind") == "feed_detection":
            feed_detections[str(row["trade_key"])][str(row["feed"])] = int(row["observed_at_ms"])

    def group(rows: list[dict[str, Any]]) -> dict[str, Any]:
        executable = [row for row in rows if row.get("paper_quote", {}).get("executable")]
        slippage = [
            float(row["paper_quote"]["vwap"]) - float(row["trade"]["price"])
            for row in rows
            if row.get("paper_quote", {}).get("vwap") is not None
        ]
        return {
            "buy_signals": len(rows),
            "depth_filled": sum(bool(row["paper_quote"].get("filled")) for row in rows),
            "minimum_size_met": sum(
                bool(row["paper_quote"].get("meets_min_order_size")) for row in rows
            ),
            "executable_quotes": len(executable),
            "fees_usd": round(sum(float(row["paper_quote"]["fee_usd"]) for row in rows), 8),
            "public_detection_lag_ms": metric_summary(
                [float(row["public_detection_lag_ms"]) for row in rows]
            ),
            "sample_delay_ms": metric_summary(
                [float(row["actual_sample_delay_ms"]) for row in rows]
            ),
            "entry_slippage_vs_leader": metric_summary(slippage),
        }

    timestamps = [int(row["observed_at_ms"]) for row in events if row.get("observed_at_ms")]
    challenger_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in quotes:
        challengers = row.get("paper_challengers") or {}
        for level, quote in (challengers.get("fixed_slippage_c") or {}).items():
            if quote:
                challenger_rows[f"slippage_{level}c"].append(dict(row, paper_quote=quote))
        minimum = (challengers.get("venue_minimum") or {}).get("quote")
        if minimum:
            challenger_rows["venue_minimum_under_5usd"].append(dict(row, paper_quote=minimum))

    paired_feed_lags = [
        sources["activity"] - sources["trades"]
        for sources in feed_detections.values()
        if "activity" in sources and "trades" in sources
    ]
    return {
        "schema": "copybot.wallet-observation-summary.v1",
        "event_count": len(events),
        "event_kinds": dict(sorted(kinds.items())),
        "observation_window_ms": {
            "first": min(timestamps) if timestamps else None,
            "last": max(timestamps) if timestamps else None,
            "duration": max(timestamps) - min(timestamps) if timestamps else 0,
        },
        "all_quotes": group(quotes),
        "by_market_category": {
            key: group(rows) for key, rows in sorted(category_rows.items())
        },
        "by_lane": {key: group(rows) for key, rows in sorted(lane_rows.items())},
        "challengers": {key: group(rows) for key, rows in sorted(challenger_rows.items())},
        "feed_detection": {
            "first_seen": dict(sorted(Counter(
                min(sources, key=sources.get) for sources in feed_detections.values() if sources
            ).items())),
            "paired_trade_count": len(paired_feed_lags),
            "activity_minus_trades_ms": metric_summary(paired_feed_lags),
        },
        "outcome_status": (
            "No realised or resolved outcome is inferred. "
            "Entry observations alone cannot prove profitability."
        ),
        "promotion_threshold": None,
        "order_capability": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", type=Path, required=True)
    result.add_argument("--output", type=Path)
    return result


def main(args: argparse.Namespace) -> int:
    with args.input.open(encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    summary = summarise(events)
    encoded = json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parser().parse_args()))
