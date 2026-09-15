#!/usr/bin/env python3
"""Credential-free settlement watcher for executable paper wallet quotes."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

RPC = "https://polygon-bor-rpc.publicnode.com"
CLOB = "https://clob.polymarket.com"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
DEN = "0xdd34de67"
NUM = "0x0504c814"


def get_json(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "copybot-paper-settler/1"})
    with urlopen(req, timeout=12) as response:
        return json.load(response)


def rpc(data: str) -> int | None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": CTF, "data": data}, "latest"]}).encode()
    req = Request(RPC, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=12) as response:
            result = json.load(response).get("result")
        return int(result, 16) if isinstance(result, str) and result.startswith("0x") else None
    except Exception:
        return None


def payout(condition: str, token: str, market_cache: dict[str, dict]) -> float | None:
    condition = condition.lower()
    raw = condition[2:] if condition.startswith("0x") else condition
    if len(raw) != 64:
        return None
    market = market_cache.get(condition)
    if market is None:
        try:
            market = get_json(f"{CLOB}/clob-markets/0x{raw}")
        except Exception:
            return None
        market_cache[condition] = market
    index = next((i for i, row in enumerate(market.get("tokens") or [])
                  if str(row.get("token_id")) == str(token)), None)
    if index is None:
        return None
    denominator = rpc(DEN + raw)
    numerator = rpc(NUM + raw + format(index, "064x")) if denominator else None
    if not denominator or numerator is None:
        return None
    value = numerator / denominator
    return value if 0 <= value <= 1 else None


def key(source: str, row: dict, variant: str) -> str:
    identity = (source, row.get("lane"), row.get("transaction_hash") or
                (row.get("trade") or {}).get("transactionHash"),
                row.get("token_id") or (row.get("trade") or {}).get("asset"), variant)
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def quotes(path: Path, source: str):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if source == "main" and row.get("kind") == "follower_quote":
                trade = row.get("trade") or {}
                condition, token = trade.get("conditionId"), trade.get("asset")
                variants = {"control": row.get("paper_quote")}
                variants.update({f"slippage_{level}c": quote for level, quote in
                                 (row.get("paper_challengers") or {}).get("fixed_slippage_c", {}).items()})
                minimum = (row.get("paper_challengers") or {}).get("venue_minimum", {}).get("quote")
                variants["venue_minimum_under_5usd"] = minimum
            elif source == "mempool" and row.get("kind") == "mempool_quote":
                condition, token = row.get("condition_id"), row.get("token_id")
                challengers = row.get("paper_challengers") or {}
                variants = {"mempool_strict": row.get("paper_quote"),
                            "mempool_uncapped": challengers.get("uncapped"),
                            "mempool_uncapped_minimum": challengers.get("uncapped_minimum")}
            else:
                continue
            for variant, quote in variants.items():
                if quote and quote.get("executable") and condition and token:
                    yield key(source, row, variant), row, variant, str(condition), str(token), quote


def run(args: argparse.Namespace) -> int:
    state = json.loads(args.state.read_text()) if args.state.exists() else {"settled": [], "markets": {}}
    settled = set(state.get("settled") or [])
    markets = state.get("markets") or {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if args.output.exists():
        with args.output.open(encoding="utf-8") as handle:
            existing = {json.loads(line).get("key") for line in handle}
    settled.update(existing - {None})
    added = 0
    for source, path in (("main", args.observations), ("mempool", args.mempool)):
        for row_key, row, variant, condition, token, quote in quotes(path, source):
            if row_key in settled:
                continue
            value = payout(condition, token, markets)
            if value is None:
                continue
            shares = float(quote.get("shares") or 0)
            total = float(quote.get("total_usd") or 0)
            event = {"schema": "copybot.paper-settlement.v1", "kind": "paper_settlement",
                     "key": row_key, "source": source, "variant": variant,
                     "lane": row.get("lane"), "condition_id": condition, "token_id": token,
                     "resolved_at_ms": int(time.time() * 1000), "payout_per_share": value,
                     "shares": shares, "cost_usd": total,
                     "pnl_usd": round(shares * value - total, 8), "order_capability": False}
            with args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            settled.add(row_key)
            added += 1
    state["settled"] = sorted(settled)[-100000:]
    state["markets"] = markets
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"settled_added": added, "settled_total": len(settled)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--mempool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    while True:
        run(args)
        if args.interval_seconds <= 0:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
