#!/usr/bin/env python3
"""Read-only HTTP summary for the isolated paper wallet observer."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from threading import Lock
import time

from evaluate_observations import summarise


def metric(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": round(min(values), 8) if values else None,
        "median": round(median(values), 8) if values else None,
        "max": round(max(values), 8) if values else None,
    }


class QuoteGroup:
    def __init__(self) -> None:
        self.buy_signals = 0
        self.depth_filled = 0
        self.minimum_size_met = 0
        self.executable_quotes = 0
        self.fees_usd = 0.0
        self.detection_lags: list[float] = []
        self.sample_delays: list[float] = []
        self.slippage: list[float] = []

    def add(self, row: dict[str, object], quote: dict[str, object]) -> None:
        self.buy_signals += 1
        self.depth_filled += bool(quote.get("filled"))
        self.minimum_size_met += bool(quote.get("meets_min_order_size"))
        self.executable_quotes += bool(quote.get("executable"))
        self.fees_usd += float(quote.get("fee_usd") or 0)
        self.detection_lags.append(float(row.get("public_detection_lag_ms") or 0))
        self.sample_delays.append(float(row.get("actual_sample_delay_ms") or 0))
        if quote.get("vwap") is not None:
            trade = row.get("trade") or {}
            self.slippage.append(float(quote["vwap"]) - float(trade.get("price") or 0))

    def value(self) -> dict[str, object]:
        return {
            "buy_signals": self.buy_signals,
            "depth_filled": self.depth_filled,
            "minimum_size_met": self.minimum_size_met,
            "executable_quotes": self.executable_quotes,
            "fees_usd": round(self.fees_usd, 8),
            "public_detection_lag_ms": metric(self.detection_lags),
            "sample_delay_ms": metric(self.sample_delays),
            "entry_slippage_vs_leader": metric(self.slippage),
        }


class IncrementalSummary:
    def __init__(self) -> None:
        self.event_count = 0
        self.kinds: Counter[str] = Counter()
        self.last_observed_at_ms: int | None = None
        self.control = QuoteGroup()
        self.challengers: dict[str, QuoteGroup] = defaultdict(QuoteGroup)
        self.feed_sources: dict[str, dict[str, int]] = defaultdict(dict)
        self.mempool_count = 0
        self.mempool_last_ms: int | None = None
        self.mempool_sources: set[str] = set()
        self.mempool_quotes = QuoteGroup()
        self.mempool_uncapped_quotes = QuoteGroup()
        self.mempool_quote_errors = 0
        self.mempool_detected_by_hash: dict[str, int] = {}
        self.public_detected_by_hash: dict[str, int] = {}
        self.joined_hashes: set[str] = set()
        self.mempool_leads: list[float] = []

    def _join_hash(self, transaction_hash: str) -> None:
        if not transaction_hash or transaction_hash in self.joined_hashes:
            return
        mempool_ms = self.mempool_detected_by_hash.get(transaction_hash)
        public_ms = self.public_detected_by_hash.get(transaction_hash)
        if mempool_ms is None or public_ms is None:
            return
        self.joined_hashes.add(transaction_hash)
        self.mempool_leads.append(float(public_ms - mempool_ms))

    def add_main(self, row: dict[str, object]) -> None:
        kind = str(row.get("kind"))
        self.event_count += 1
        self.kinds[kind] += 1
        if row.get("observed_at_ms") is not None:
            observed = int(row["observed_at_ms"])
            self.last_observed_at_ms = max(self.last_observed_at_ms or observed, observed)
        if kind == "feed_detection":
            self.feed_sources[str(row["trade_key"])][str(row["feed"])] = int(row["observed_at_ms"])
        if kind != "follower_quote":
            return
        trade = row.get("trade") or {}
        transaction_hash = str(trade.get("transactionHash") or "").lower()
        detected_at_ms = int(row.get("detected_at_ms") or 0)
        if transaction_hash and detected_at_ms:
            self.public_detected_by_hash[transaction_hash] = min(
                self.public_detected_by_hash.get(transaction_hash, detected_at_ms), detected_at_ms
            )
            self._join_hash(transaction_hash)
        quote = row.get("paper_quote") or {}
        self.control.add(row, quote)
        challengers = row.get("paper_challengers") or {}
        for level, value in (challengers.get("fixed_slippage_c") or {}).items():
            if value:
                self.challengers[f"slippage_{level}c"].add(row, value)
        minimum = (challengers.get("venue_minimum") or {}).get("quote")
        if minimum:
            self.challengers["venue_minimum_under_5usd"].add(row, minimum)

    def add_mempool(self, row: dict[str, object]) -> None:
        kind = str(row.get("kind"))
        if row.get("observed_at_ms") is not None:
            observed = int(row["observed_at_ms"])
            self.mempool_last_ms = max(self.mempool_last_ms or observed, observed)
        self.mempool_sources.add(str(row.get("source")))
        if kind == "mempool_detection":
            self.mempool_count += 1
            transaction_hash = str(row.get("transaction_hash") or "").lower()
            if transaction_hash and row.get("observed_at_ms") is not None:
                observed = int(row["observed_at_ms"])
                self.mempool_detected_by_hash[transaction_hash] = min(
                    self.mempool_detected_by_hash.get(transaction_hash, observed), observed
                )
                self._join_hash(transaction_hash)
        elif kind == "mempool_quote":
            adapted = dict(row)
            adapted["public_detection_lag_ms"] = 0
            adapted["trade"] = {"price": row.get("leader_price") or 0}
            self.mempool_quotes.add(adapted, row.get("paper_quote") or {})
            uncapped = (row.get("paper_challengers") or {}).get("uncapped")
            if uncapped:
                self.mempool_uncapped_quotes.add(adapted, uncapped)
        elif kind == "mempool_quote_error":
            self.mempool_quote_errors += 1

    def value(self, now_ms: int) -> dict[str, object]:
        paired = [
            sources["activity"] - sources["trades"]
            for sources in self.feed_sources.values()
            if "activity" in sources and "trades" in sources
        ]
        first_seen = Counter(
            min(sources, key=sources.get) for sources in self.feed_sources.values() if sources
        )
        return {
            "schema": "copybot.wallet-dashboard-summary.v1",
            "generated_at_ms": now_ms,
            "last_observed_at_ms": self.last_observed_at_ms,
            "event_count": self.event_count,
            "event_kinds": dict(sorted(self.kinds.items())),
            "control": self.control.value(),
            "challengers": {key: value.value() for key, value in sorted(self.challengers.items())},
            "feed_detection": {
                "first_seen": dict(sorted(first_seen.items())),
                "paired_trade_count": len(paired),
                "activity_minus_trades_ms": metric([float(value) for value in paired]),
            },
            "mempool": {
                "detection_count": self.mempool_count,
                "last_observed_at_ms": self.mempool_last_ms,
                "sources": sorted(self.mempool_sources),
                "book_samples": self.mempool_quotes.value(),
                "uncapped_book_samples": self.mempool_uncapped_quotes.value(),
                "book_sample_errors": self.mempool_quote_errors,
                "public_feed_hash_matches": len(self.joined_hashes),
                "lead_vs_public_detection_ms": metric(self.mempool_leads),
                "order_capability": False,
            },
            "order_capability": False,
            "profitability_status": "unknown_no_settled_outcomes",
        }


class SummaryCache:
    def __init__(self, observations: Path, mempool_observations: Path | None) -> None:
        self.paths = ((observations, "main"), (mempool_observations, "mempool"))
        self.offsets: dict[Path, int] = {}
        self.summary = IncrementalSummary()
        self.lock = Lock()

    def _read_new(self, source: Path, kind: str) -> None:
        if not source.exists():
            return
        offset = self.offsets.get(source, 0)
        if source.stat().st_size < offset:
            raise RuntimeError(f"append-only source shrank: {source}")
        with source.open("rb") as handle:
            handle.seek(offset)
            while True:
                before = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(before)
                    break
                row = json.loads(line)
                if kind == "main":
                    self.summary.add_main(row)
                else:
                    self.summary.add_mempool(row)
            self.offsets[source] = handle.tell()

    def value(self, now_ms: int) -> dict[str, object]:
        with self.lock:
            for source, kind in self.paths:
                if source is not None:
                    self._read_new(source, kind)
            return self.summary.value(now_ms)


def build_summary(
    events: list[dict[str, object]], now_ms: int,
    mempool_events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    summary = summarise(events)
    mempool_events = mempool_events or []
    mempool_times = [
        int(row["observed_at_ms"])
        for row in mempool_events if row.get("observed_at_ms") is not None
    ]
    return {
        "schema": "copybot.wallet-dashboard-summary.v1",
        "generated_at_ms": now_ms,
        "last_observed_at_ms": summary["observation_window_ms"]["last"],
        "event_count": summary["event_count"],
        "event_kinds": summary["event_kinds"],
        "control": summary["all_quotes"],
        "challengers": summary["challengers"],
        "feed_detection": summary["feed_detection"],
        "mempool": {
            "detection_count": len(mempool_events),
            "last_observed_at_ms": max(mempool_times) if mempool_times else None,
            "sources": sorted({str(row.get("source")) for row in mempool_events}),
            "order_capability": False,
        },
        "order_capability": False,
        "profitability_status": "unknown_no_settled_outcomes",
    }


class Handler(BaseHTTPRequestHandler):
    observations = Path("observations.jsonl")
    mempool_observations: Path | None = None
    cache: SummaryCache | None = None

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/summary":
            self.send_error(404)
            return
        try:
            if self.cache is None:
                raise RuntimeError("summary cache is not configured")
            payload = self.cache.value(int(time.time() * 1000))
            encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except Exception as error:
            encoded = json.dumps({"error": f"{type(error).__name__}: {error}"}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--mempool-observations", type=Path)
    args = parser.parse_args()
    Handler.observations = args.observations
    Handler.mempool_observations = args.mempool_observations
    Handler.cache = SummaryCache(args.observations, args.mempool_observations)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
