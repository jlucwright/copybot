#!/usr/bin/env python3
"""Refresh the frozen paper-only wallet candidate cohort from public APIs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.parse import urlencode

from observe_wallets import DATA_API, fetch_json


def protocol_digest(protocol: dict[str, Any]) -> str:
    encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def leaderboard_url(category: str, protocol: dict[str, Any]) -> str:
    query = urlencode(
        {
            "category": category,
            "timePeriod": protocol["leaderboard_period"],
            "orderBy": protocol["leaderboard_order"],
            "limit": protocol["leaderboard_limit_per_category"],
            "offset": 0,
        }
    )
    return f"{DATA_API}/v1/leaderboard?{query}"


def activity_summary(trades: list[dict[str, Any]], captured_at_s: int) -> dict[str, Any]:
    timestamps = [int(row["timestamp"]) for row in trades]
    seven_days_ago = captured_at_s - 7 * 86400
    recent = [row for row in trades if int(row["timestamp"]) >= seven_days_ago]
    return {
        "sample_count": len(trades),
        "latest_trade_timestamp_s": max(timestamps) if timestamps else None,
        "latest_trade_age_s": captured_at_s - max(timestamps) if timestamps else None,
        "trades_in_sample_from_last_7d": len(recent),
        "distinct_markets_in_sample_from_last_7d": len(
            {row.get("conditionId") for row in recent if row.get("conditionId")}
        ),
        "buy_count_in_sample_from_last_7d": sum(row.get("side") == "BUY" for row in recent),
        "sell_count_in_sample_from_last_7d": sum(row.get("side") == "SELL" for row in recent),
    }


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh(protocol: dict[str, Any], captured_at_s: int) -> tuple[dict[str, Any], dict[str, Any]]:
    categories = protocol["leaderboard_categories"]
    with ThreadPoolExecutor(max_workers=len(categories)) as pool:
        rows_by_category = dict(
            zip(
                categories,
                pool.map(
                    lambda category: fetch_json(leaderboard_url(category, protocol)),
                    categories,
                ),
            )
        )

    candidates: dict[str, dict[str, Any]] = {}
    for category in categories:
        rows = rows_by_category[category]
        if not isinstance(rows, list):
            raise ValueError(f"leaderboard response for {category} is not a list")
        expected = int(protocol["leaderboard_limit_per_category"])
        if len(rows) != expected:
            raise ValueError(
                f"leaderboard response for {category} returned {len(rows)} rows, "
                f"expected {expected}"
            )
        for row in rows:
            wallet = str(row["proxyWallet"]).lower()
            candidate = candidates.setdefault(
                wallet,
                {
                    "wallet": wallet,
                    "user_name": row.get("userName") or "",
                    "leaderboards": [],
                },
            )
            candidate["leaderboards"].append(
                {
                    "category": category,
                    "rank": int(row["rank"]),
                    "pnl_usd": float(row["pnl"]),
                    "volume_usd": float(row["vol"]),
                }
            )

    activity_limit = int(protocol["activity_sample_limit"])
    ordered_wallets = sorted(candidates)

    def fetch_activity(wallet: str) -> list[dict[str, Any]]:
        query = urlencode(
            {"user": wallet, "type": "TRADE", "limit": activity_limit, "sortDirection": "DESC"}
        )
        result = fetch_json(f"{DATA_API}/activity?{query}")
        if not isinstance(result, list):
            raise ValueError(f"activity response for {wallet} is not a list")
        return result

    with ThreadPoolExecutor(max_workers=min(12, len(ordered_wallets))) as pool:
        activities = dict(zip(ordered_wallets, pool.map(fetch_activity, ordered_wallets)))
    for wallet in ordered_wallets:
        candidates[wallet]["activity"] = activity_summary(activities[wallet], captured_at_s)

    digest = protocol_digest(protocol)
    captured_at = datetime.fromtimestamp(captured_at_s, timezone.utc).isoformat()
    snapshot = {
        "schema": "copybot.wallet-candidate-snapshot.v1",
        "captured_at": captured_at,
        "captured_at_s": captured_at_s,
        "protocol_sha256": digest,
        "protocol": protocol,
        "candidates": [candidates[wallet] for wallet in ordered_wallets],
    }
    sizing = protocol["paper_sizing"]
    challengers = protocol["paper_challengers"]
    roster = {
        "schema": "copybot.wallet-observer-roster.v1",
        "generated_at": captured_at,
        "protocol_sha256": digest,
        "lanes": [
            {
                "name": f"candidate_{wallet[2:10]}",
                "wallet": wallet,
                "leaderboard_categories": sorted(
                    row["category"] for row in candidates[wallet]["leaderboards"]
                ),
                **sizing,
                "paper_challengers": challengers,
            }
            for wallet in ordered_wallets
        ],
    }
    return snapshot, roster


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--protocol", type=Path, default=Path("experiment/protocol.json"))
    result.add_argument("--snapshot", type=Path, required=True)
    result.add_argument("--roster", type=Path, required=True)
    return result


def main(args: argparse.Namespace) -> int:
    with args.protocol.open(encoding="utf-8") as handle:
        protocol = json.load(handle)
    snapshot, roster = refresh(protocol, int(time.time()))
    save_json(args.snapshot, snapshot)
    save_json(args.roster, roster)
    print(
        json.dumps(
            {
                "captured_at": snapshot["captured_at"],
                "candidate_count": len(snapshot["candidates"]),
                "categories": protocol["leaderboard_categories"],
                "protocol_sha256": snapshot["protocol_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parser().parse_args()))
