#!/usr/bin/env python3
"""Read-only HTTP summary for the isolated paper wallet observer."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time

from evaluate_observations import summarise


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

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/summary":
            self.send_error(404)
            return
        try:
            with self.observations.open(encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle if line.strip()]
            mempool_events = []
            if self.mempool_observations and self.mempool_observations.exists():
                with self.mempool_observations.open(encoding="utf-8") as handle:
                    mempool_events = [json.loads(line) for line in handle if line.strip()]
            payload = build_summary(events, int(time.time() * 1000), mempool_events)
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
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
