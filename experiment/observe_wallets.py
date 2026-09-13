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
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
USER_AGENT = "copybot-paper-observer/2"
FEEDS = ("trades", "activity")
MAX_FEED_WORKERS = 64
SLIPPAGE_LEVELS_C = (0.15, 0.5, 1.0)
VENUE_MINIMUM_CAP_USD = 5.0


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


def normalise_v2_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Convert either public v2 feed to the observer's stable internal shape."""
    size = float(row.get("size") or 0.0)
    price = float(row.get("price") or 0.0)
    return {
        "proxyWallet": row.get("proxy_wallet"),
        "transactionHash": row.get("transaction_hash"),
        "timestamp": row.get("timestamp"),
        "conditionId": row.get("condition_id"),
        "asset": row.get("token_id"),
        "side": row.get("side"),
        "size": size,
        "usdcSize": float(row.get("usdc_size") or size * price),
        "price": price,
        "title": row.get("title"),
        "slug": row.get("slug"),
        "eventSlug": row.get("event_slug"),
        "outcome": row.get("outcome"),
    }


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
    try:
        import tomllib
    except ImportError as error:
        raise RuntimeError("TOML config input requires Python 3.11 or newer") from error
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
                "slippage_levels_c": tuple(float(value) for value in
                                           lane.get("paper_challengers", {}).get(
                                               "fixed_slippage_c", SLIPPAGE_LEVELS_C)),
                "venue_minimum_cap_usd": float(lane.get("paper_challengers", {}).get(
                    "venue_minimum_cap_usd", VENUE_MINIMUM_CAP_USD)),
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


def venue_minimum_cash(
    asks: list[dict[str, Any]], min_order_size: float, fee_rate: float, cap_usd: float
) -> float | None:
    """Return the cash needed for the exact share minimum, or fail closed above cap."""
    if min_order_size <= 0:
        return None
    remaining = min_order_size
    total = 0.0
    for level in sorted(asks, key=lambda row: float(row["price"])):
        price = float(level["price"])
        take = min(remaining, float(level["size"]))
        if price <= 0 or take <= 0:
            continue
        total += take * price + taker_fee(take, price, fee_rate)
        remaining -= take
        if remaining <= 1e-9:
            return total if total <= cap_usd + 1e-9 else None
    return None


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
    if state.get("version") not in (1, 2, 3) or not isinstance(state.get("seen"), list):
        raise ValueError("observer state is invalid")
    if state.get("version") == 2 and not isinstance(state.get("baselined_wallets"), list):
        raise ValueError("observer state is invalid")
    return state


def state_baselined_wallets(state: dict[str, Any] | None) -> set[str]:
    if not state or state["version"] == 1:
        return set()
    return set(state["baselined_wallets"])


def state_feed_seen(state: dict[str, Any] | None) -> dict[str, set[str]]:
    if not state or state.get("version") != 3:
        return {feed: set() for feed in FEEDS}
    value = state.get("feed_seen")
    if not isinstance(value, dict) or any(not isinstance(value.get(feed), list) for feed in FEEDS):
        raise ValueError("observer state is invalid")
    return {feed: set(value[feed]) for feed in FEEDS}


def state_baselined_feed_wallets(state: dict[str, Any] | None) -> set[str]:
    if not state or state.get("version") != 3:
        return set()
    value = state.get("baselined_feed_wallets")
    if not isinstance(value, list):
        raise ValueError("observer state is invalid")
    return set(value)


def feed_url(feed: str, wallet: str, limit: int) -> str:
    if feed not in FEEDS:
        raise ValueError(f"unsupported feed: {feed}")
    query = urlencode({"user": wallet, "limit": limit})
    return f"{DATA_API}/v2/{feed}?{query}"


def lane_accepts(trade: dict[str, Any], lane: dict[str, Any]) -> bool:
    required = lane.get("title_contains", "").strip().casefold()
    return not required or required in str(trade.get("title") or "").casefold()


def quote_trade(
    trade: dict[str, Any], lane: dict[str, Any], clob_host: str, delay_ms: int,
    detected_at_ms: int, detected_source: str,
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
    asks = book.get("asks") or []
    minimum = float(market.get("mos") or 0.0)
    quote = None
    challengers = None
    if side == "BUY":
        quote = buy_quote(
            asks, cash, fee_rate, min_order_size=minimum,
            max_price=min(1.0, leader_price + lane["buy_slippage_c"] / 100.0),
        )
        slippage_quotes = {
            str(level): buy_quote(
                asks, cash, fee_rate, min_order_size=minimum,
                max_price=min(1.0, leader_price + level / 100.0),
            )
            for level in lane.get("slippage_levels_c", SLIPPAGE_LEVELS_C)
        }
        minimum_cap = lane.get("venue_minimum_cap_usd", VENUE_MINIMUM_CAP_USD)
        minimum_cash = venue_minimum_cash(asks, minimum, fee_rate, minimum_cap)
        minimum_quote = (
            buy_quote(
                asks, minimum_cash, fee_rate, min_order_size=minimum,
                max_price=min(1.0, leader_price + lane["buy_slippage_c"] / 100.0),
            )
            if minimum_cash is not None else None
        )
        challengers = {
            "same_book_snapshot": True,
            "fixed_slippage_c": slippage_quotes,
            "venue_minimum": {
                "cap_usd": minimum_cap,
                "original_requested_cash_usd": round(cash, 8),
                "requested_cash_usd": round(minimum_cash, 8) if minimum_cash is not None else None,
                "oversize_vs_control_usd": round(max(0.0, minimum_cash - cash), 8)
                if minimum_cash is not None else None,
                "quote": minimum_quote,
                "eligible_under_cap": minimum_cash is not None,
            },
        }
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
        "schema": "copybot.public-wallet-observation.v2",
        "kind": "follower_quote" if quote is not None else "unsupported_sell",
        "observed_at_ms": sampled_at_ms,
        "detected_at_ms": detected_at_ms,
        "detected_source": detected_source,
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
        "paper_challengers": challengers,
        "limitations": [
            "public feed visibility is later than the leader match",
            "book sampling is not an order or fill",
            "sell copying needs independently tracked follower inventory",
        ],
    }


def run(args: argparse.Namespace) -> int:
    lanes = roster_specs(args.roster) if args.roster else lane_specs(args.config)
    state = load_state(args.state)
    seen = set(state["seen"] if state else [])
    feed_seen = state_feed_seen(state)
    feed_seen_order = {
        feed: list(state["feed_seen"][feed])
        if state and state.get("version") == 3 else []
        for feed in FEEDS
    }
    # Older state did not distinguish feeds. Re-baseline each feed and wallet
    # rather than misclassify migration history as forward observations.
    baselined_feed_wallets = state_baselined_feed_wallets(state)
    polls = 0
    while args.max_polls == 0 or polls < args.max_polls:
        newly_seen: list[str] = []
        new_trades: list[tuple[dict[str, Any], dict[str, Any], int, str]] = []
        fetch_errors: list[tuple[dict[str, Any], str, Exception]] = []
        newly_baselined: list[str] = []

        def fetch_lane_feed(item: tuple[dict[str, Any], str]) -> tuple[dict[str, Any], str, Any, int]:
            lane, feed = item
            try:
                response = fetch_json(feed_url(feed, lane["wallet"], args.limit))
                rows = response.get("data") if isinstance(response, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("response data is not a list")
                trades = [normalise_v2_trade(row) for row in rows]
            except Exception as error:
                return lane, feed, error, int(time.time() * 1000)
            return lane, feed, trades, int(time.time() * 1000)

        inputs = [(lane, feed) for lane in lanes for feed in FEEDS]
        with ThreadPoolExecutor(max_workers=min(MAX_FEED_WORKERS, len(inputs))) as activity_pool:
            fetched = list(activity_pool.map(fetch_lane_feed, inputs))
        for lane, feed, trades, detected_at_ms in fetched:
            if isinstance(trades, Exception):
                fetch_errors.append((lane, feed, trades))
                continue
            baseline_key = f"{feed}:{lane['wallet']}"
            needs_baseline = baseline_key not in baselined_feed_wallets
            for trade in reversed(trades):
                key = trade_key(trade)
                if key in feed_seen[feed]:
                    continue
                feed_seen[feed].add(key)
                feed_seen_order[feed].append(key)
                if not needs_baseline:
                    append_event(args.output, {
                        "schema": "copybot.public-wallet-observation.v2",
                        "kind": "feed_detection",
                        "observed_at_ms": detected_at_ms,
                        "lane": lane["name"],
                        "trade_key": key,
                        "feed": feed,
                    })
                    if key not in seen and key not in newly_seen and lane_accepts(trade, lane):
                        newly_seen.append(key)
                        new_trades.append((trade, lane, detected_at_ms, feed))
            if needs_baseline:
                newly_baselined.append(baseline_key)
        baseline_errors = [
            f"{lane['name']}:{feed}" for lane, feed, _ in fetch_errors
            if f"{feed}:{lane['wallet']}" not in baselined_feed_wallets
        ]
        if baseline_errors:
            names = ", ".join(baseline_errors)
            raise RuntimeError(f"cannot establish complete no-backfill baseline: {names}")
        seen.update(newly_seen)
        baselined_feed_wallets.update(newly_baselined)
        if newly_baselined:
            append_event(
                args.output,
                {
                    "schema": "copybot.public-wallet-observation.v2",
                    "kind": "baseline",
                    "observed_at_ms": int(time.time() * 1000),
                    "feed_wallets": sorted(newly_baselined),
                    "existing_trades_suppressed": len(newly_seen),
                },
            )
        else:
            for lane, feed, error in fetch_errors:
                append_event(
                    args.output,
                    {
                        "schema": "copybot.public-wallet-observation.v2",
                        "kind": "feed_error",
                        "observed_at_ms": int(time.time() * 1000),
                        "lane": lane["name"],
                        "feed": feed,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
            with ThreadPoolExecutor(max_workers=min(32, max(1, len(new_trades)))) as pool:
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
                            detected_source,
                        ),
                    )
                    for trade, lane, detected_at_ms, detected_source in new_trades
                ]
                for trade, lane, future in futures:
                    try:
                        append_event(args.output, future.result())
                    except Exception as error:
                        append_event(
                            args.output,
                            {
                                "schema": "copybot.public-wallet-observation.v2",
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
            "version": 3,
            "updated_at_ms": int(time.time() * 1000),
            "seen": ordered[:5000],
            "baselined_feed_wallets": sorted(baselined_feed_wallets),
            "feed_seen": {feed: feed_seen_order[feed][-5000:] for feed in FEEDS},
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
