#!/usr/bin/env python3
"""Read-only HTTP summary for the isolated paper wallet observer."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time

from evaluate_observations import summarise


def build_summary(events: list[dict[str, object]], now_ms: int) -> dict[str, object]:
    summary = summarise(events)
    return {
        "schema": "copybot.wallet-dashboard-summary.v1",
        "generated_at_ms": now_ms,
        "last_observed_at_ms": summary["observation_window_ms"]["last"],
        "event_count": summary["event_count"],
        "event_kinds": summary["event_kinds"],
        "control": summary["all_quotes"],
        "challengers": summary["challengers"],
        "feed_detection": summary["feed_detection"],
        "order_capability": False,
        "profitability_status": "unknown_no_settled_outcomes",
    }


class Handler(BaseHTTPRequestHandler):
    observations = Path("observations.jsonl")

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/summary":
            self.send_error(404)
            return
        try:
            with self.observations.open(encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle if line.strip()]
            payload = build_summary(events, int(time.time() * 1000))
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
    args = parser.parse_args()
    Handler.observations = args.observations
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
