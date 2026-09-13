#!/usr/bin/env python3
"""Public, order-incapable wallet-copy feasibility observer.

The observer deliberately starts from a no-backfill baseline.  For each trade
that becomes visible afterwards it waits for the configured follower delay,
then samples the public CLOB book and records whether a small taker copy would
have been fillable, including the market's own fee curve.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
import tomllib
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
USER_AGENT = "copybot-paper-observer/1"


def fetch_json(url: str, *, timeout: float = 10.0, attempts: int = 4) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt + 1 == attempts:
                raise
            retry_after = error.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after is not None else 2 ** attempt
            except ValueError:
                delay = 2 ** attempt
            time.sleep(max(1.0, min(delay, 30.0)))
    raise RuntimeError("unreachable")


def trade_key(trade: dict[str, Any]) -> str:
    fields = (
        trade.get("proxyWallet"),
        trade.get("transactionHash"),
        trade.get("timestamp"),
        trade.get("conditionId"),
        trade.get("asset"),
        trade.get("side"),
        trade.get("size"),
        trade.get("price"),
    )
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def taker_fee(shares: float, price: float, rate: float) -> float:
    if not all(math.isfinite(value) for value in (shares, price, rate)):
        raise ValueError("fee inputs must be finite")
    if shares < 0 or not 0 <= price <= 1 or rate < 0:
        raise ValueError("fee inputs are outside their valid range")
    return shares * rate * price * (1 - price)


def buy_quote(
    asks: list[dict[str, Any]],
    cash: float,
    fee_rate: float,
    *,
    min_order_size: float = 0.0,
    max_price: float = 1.0,
) -> dict[str, Any]:
    """Walk asks using a cash cap inclusive of taker fees."""
    remaining = cash
    shares = cost = fee = 0.0
    levels = 0
    for level in sorted(asks, key=lambda row: float(row["price"])):
        price = float(level["price"])
        available = float(level["size"])
        if price > max_price:
            break
        unit_total = price + fee_rate * price * (1 - price)
        if price <= 0 or available <= 0 or unit_total <= 0:
            continue
        take = min(available, remaining / unit_total)
        if take <= 0:
            break
        level_cost = take * price
        level_fee = taker_fee(take, price, fee_rate)
        shares += take
        cost += level_cost
        fee += level_fee
        remaining -= level_cost + level_fee
        levels += 1
        if remaining < 1e-9:
            break
    total = cost + fee
    depth_filled = total >= cash - 1e-6
    meets_min_order_size = shares >= min_order_size
    return {
        "requested_cash_usd": round(cash, 8),
        "filled": depth_filled,
        "meets_min_order_size": meets_min_order_size,
        "executable": depth_filled and meets_min_order_size,
        "shares": round(shares, 8),
        "notional_usd": round(cost, 8),
        "fee_usd": round(fee, 8),
        "total_usd": round(total, 8),
        "vwap": round(cost / shares, 8) if shares else None,
        "levels": levels,
        "unspent_usd": round(max(0.0, remaining), 8),
        "max_price": round(max_price, 8),
    }


def lane_specs(config_path: Path) -> list[dict[str, Any]]:
    with config_path.open("rb") as handle:
        root = tomllib.load(handle)
    if root.get("bot", {}).get("mode") != "dry":
        raise ValueError("observer requires bot.mode = 'dry'")
    lanes = []
    for lane in root.get("lane", []):
        if not lane.get("enabled", False):
            continue
        sizing = lane["sizing"]
        execution = lane["execution"]
        if execution.get("mode", "taker") != "taker" or execution.get("copy_makers", False):
            raise ValueError(f"lane {lane['name']} is not a taker-only paper lane")
        lanes.append(
            {
                "name": lane["name"],
                "wallet": lane["wallet"].lower(),
                "pct": float(sizing["pct"]),
                "max_usd": float(sizing["max_usd_per_fill"]),
                "min_usd": float(sizing["min_order_usd"]),
                "min_fill_floor": bool(sizing.get("min_fill_floor", False)),
                "title_contains": str(lane.get("observer", {}).get("title_contains") or ""),
                "buy_slippage_c": float(execution.get("buy_slippage_c", 0.0)),
                "leaderboard_categories": [],
            }
        )
    if not lanes:
        raise ValueError("observer has no enabled lanes")
    return lanes


def roster_specs(roster_path: Path) -> list[dict[str, Any]]:
    with roster_path.open(encoding="utf-8") as handle:
        root = json.load(handle)
    if root.get("schema") != "copybot.wallet-observer-roster.v1":
        raise ValueError("observer roster schema is invalid")
    lanes = []
    for lane in root.get("lanes", []):
        wallet = str(lane["wallet"]).lower()
        if not wallet.startswith("0x") or len(wallet) != 42:
            raise ValueError(f"lane {lane.get('name')} has an invalid wallet")
        lanes.append(
            {
                "name": str(lane["name"]),
                "wallet": wallet,
                "pct": float(lane["pct"]),
                "max_usd": float(lane["max_usd_per_fill"]),
                "min_usd": float(lane["min_order_usd"]),
                "min_fill_floor": bool(lane.get("min_fill_floor", False)),
                "title_contains": "",
                "buy_slippage_c": float(lane["buy_slippage_c"]),
                "leaderboard_categories": list(lane.get("leaderboard_categories") or []),
            }
        )
    if not lanes:
        raise ValueError("observer roster has no lanes")
    return lanes


def requested_cash(trade: dict[str, Any], lane: dict[str, Any]) -> float:
    leader_cash = float(trade.get("usdcSize") or 0.0)
    amount = min(lane["max_usd"], leader_cash * lane["pct"])
    if lane["min_fill_floor"] and 0 < amount < lane["min_usd"]:
        amount = lane["min_usd"]
    return amount


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") not in (1, 2) or not isinstance(state.get("seen"), list):
        raise ValueError("observer state is invalid")
    if state.get("version") == 2 and not isinstance(state.get("baselined_wallets"), list):
        raise ValueError("observer state is invalid")
    return state


def state_baselined_wallets(state: dict[str, Any] | None) -> set[str]:
    if not state or state["version"] == 1:
        return set()
    return set(state["baselined_wallets"])


def activity_url(wallet: str, limit: int) -> str:
    query = urlencode(
        {"user": wallet, "type": "TRADE", "limit": limit, "sortDirection": "DESC"}
    )
    return f"{DATA_API}/activity?{query}"


def lane_accepts(trade: dict[str, Any], lane: dict[str, Any]) -> bool:
    required = lane.get("title_contains", "").strip().casefold()
    return not required or required in str(trade.get("title") or "").casefold()


def quote_trade(
    trade: dict[str, Any], lane: dict[str, Any], clob_host: str, delay_ms: int,
    detected_at_ms: int,
) -> dict[str, Any]:
    due_ms = detected_at_ms + delay_ms
    time.sleep(max(0.0, (due_ms - int(time.time() * 1000)) / 1000))
    condition = str(trade["conditionId"])
    asset = str(trade["asset"])
    # Sample the executable book first. Metadata is fetched afterwards so its
    # REST latency does not contaminate the configured follower arrival time.
    book = fetch_json(f"{clob_host}/book?{urlencode({'token_id': asset})}")
    sampled_at_ms = int(time.time() * 1000)
    market = fetch_json(f"{clob_host}/clob-markets/{condition}")
    fee_details = market.get("fd") or {}
    fee_rate = float(fee_details.get("r") or 0.0)
    side = str(trade.get("side") or "").upper()
    cash = requested_cash(trade, lane)
    leader_price = float(trade.get("price") or 0.0)
    max_price = min(1.0, leader_price + lane["buy_slippage_c"] / 100.0)
    quote = (
        buy_quote(
            book.get("asks") or [],
            cash,
            fee_rate,
            min_order_size=float(market.get("mos") or 0.0),
            max_price=max_price,
        )
        if side == "BUY"
        else None
    )
    event_slug = str(trade.get("eventSlug") or "")
    event = fetch_json(f"{GAMMA_API}/events/slug/{event_slug}") if event_slug else {}
    market_categories = sorted(
        {
            str(tag.get("slug") or "").upper()
            for tag in event.get("tags") or []
            if str(tag.get("slug") or "").upper()
            in {
                "CRYPTO",
                "SPORTS",
                "POLITICS",
                "ECONOMICS",
                "FINANCE",
                "TECH",
                "CULTURE",
                "WEATHER",
                "MENTIONS",
            }
        }
    )
    return {
        "schema": "copybot.public-wallet-observation.v1",
        "kind": "follower_quote" if quote is not None else "unsupported_sell",
        "observed_at_ms": sampled_at_ms,
        "detected_at_ms": detected_at_ms,
        "leader_timestamp_s": int(trade["timestamp"]),
        "public_detection_lag_ms": max(0, detected_at_ms - int(trade["timestamp"]) * 1000),
        "configured_follower_delay_ms": delay_ms,
        "actual_sample_delay_ms": sampled_at_ms - detected_at_ms,
        "lane": lane["name"],
        "leader": lane["wallet"],
        "leaderboard_categories": lane.get("leaderboard_categories", []),
        "market_categories": market_categories,
        "trade_key": trade_key(trade),
        "trade": {
            key: trade.get(key)
            for key in (
                "transactionHash", "conditionId", "asset", "side", "size", "usdcSize",
                "price", "title", "slug", "outcome",
            )
        },
        "market": {
            "min_order_size": market.get("mos"),
            "tick_size": market.get("mts"),
            "taker_delay_enabled": market.get("itode"),
            "fee": {
                "rate": fee_rate,
                "exponent": fee_details.get("e"),
                "taker_only": fee_details.get("to"),
                "source": "clob-markets condition metadata at sample time",
                "follower_rebate_assumed_usd": 0.0,
            },
        },
        "book": {
            "timestamp": book.get("timestamp"),
            "hash": book.get("hash"),
            "best_bid": max((float(x["price"]) for x in book.get("bids") or []), default=None),
            "best_ask": min((float(x["price"]) for x in book.get("asks") or []), default=None),
        },
        "paper_quote": quote,
        "limitations": [
            "public activity visibility is later than the leader match",
            "book sampling is not an order or fill",
            "sell copying needs independently tracked follower inventory",
        ],
    }


def run(args: argparse.Namespace) -> int:
    lanes = roster_specs(args.roster) if args.roster else lane_specs(args.config)
    state = load_state(args.state)
    seen = set(state["seen"] if state else [])
    # V1 did not record which wallets it covered. Re-baseline every current
    # lane rather than risk treating a newly added wallet as forward data.
    baselined_wallets = state_baselined_wallets(state)
    polls = 0
    while args.max_polls == 0 or polls < args.max_polls:
        newly_seen: list[str] = []
        new_trades: list[tuple[dict[str, Any], dict[str, Any], int]] = []
        fetch_errors: list[tuple[dict[str, Any], Exception]] = []
        newly_baselined: list[str] = []

        def fetch_lane(lane: dict[str, Any]) -> tuple[dict[str, Any], Any, int]:
            try:
                trades = fetch_json(activity_url(lane["wallet"], args.limit))
            except Exception as error:
                return lane, error, int(time.time() * 1000)
            return lane, trades, int(time.time() * 1000)

        with ThreadPoolExecutor(max_workers=min(16, len(lanes))) as activity_pool:
            fetched = list(activity_pool.map(fetch_lane, lanes))
        for lane, trades, detected_at_ms in fetched:
            if isinstance(trades, Exception):
                fetch_errors.append((lane, trades))
                continue
            if not isinstance(trades, list):
                raise ValueError(f"activity response for {lane['name']} is not a list")
            needs_baseline = lane["wallet"] not in baselined_wallets
            for trade in reversed(trades):
                key = trade_key(trade)
                if key in seen:
                    continue
                newly_seen.append(key)
                if not needs_baseline and lane_accepts(trade, lane):
                    new_trades.append((trade, lane, detected_at_ms))
            if needs_baseline:
                newly_baselined.append(lane["wallet"])
        baseline_errors = [
            lane["name"] for lane, _ in fetch_errors if lane["wallet"] not in baselined_wallets
        ]
        if baseline_errors:
            names = ", ".join(baseline_errors)
            raise RuntimeError(f"cannot establish complete no-backfill baseline: {names}")
        seen.update(newly_seen)
        baselined_wallets.update(newly_baselined)
        if newly_baselined:
            append_event(
                args.output,
                {
                    "schema": "copybot.public-wallet-observation.v1",
                    "kind": "baseline",
                    "observed_at_ms": int(time.time() * 1000),
                    "wallets": sorted(newly_baselined),
                    "existing_trades_suppressed": len(newly_seen),
                },
            )
        else:
            for lane, error in fetch_errors:
                append_event(
                    args.output,
                    {
                        "schema": "copybot.public-wallet-observation.v1",
                        "kind": "activity_error",
                        "observed_at_ms": int(time.time() * 1000),
                        "lane": lane["name"],
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(new_trades)))) as pool:
                futures = [
                    (
                        trade,
                        lane,
                        pool.submit(
                            quote_trade,
                            trade,
                            lane,
                            args.clob_host,
                            args.delay_ms,
                            detected_at_ms,
                        ),
                    )
                    for trade, lane, detected_at_ms in new_trades
                ]
                for trade, lane, future in futures:
                    try:
                        append_event(args.output, future.result())
                    except Exception as error:
                        append_event(
                            args.output,
                            {
                                "schema": "copybot.public-wallet-observation.v1",
                                "kind": "quote_error",
                                "observed_at_ms": int(time.time() * 1000),
                                "lane": lane["name"],
                                "trade_key": trade_key(trade),
                                "error": f"{type(error).__name__}: {error}",
                            },
                        )
        ordered = list(
            dict.fromkeys(list(reversed(newly_seen)) + (state["seen"] if state else []))
        )
        state = {
            "version": 2,
            "updated_at_ms": int(time.time() * 1000),
            "seen": ordered[:5000],
            "baselined_wallets": sorted(baselined_wallets),
        }
        save_state(args.state, state)
        polls += 1
        if args.max_polls == 0 or polls < args.max_polls:
            time.sleep(args.poll_seconds)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path)
    source.add_argument("--roster", type=Path)
    result.add_argument("--state", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--clob-host", default="https://clob.polymarket.com")
    result.add_argument("--poll-seconds", type=float, default=10.0)
    result.add_argument("--delay-ms", type=int, default=250)
    result.add_argument("--limit", type=int, default=100)
    result.add_argument("--max-polls", type=int, default=1, help="zero means run continuously")
    return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
