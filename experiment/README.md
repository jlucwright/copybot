# Paper wallet-copy experiment

The experiment's Python entry points are order-incapable. They neither import
nor accept credentials, and they make public GET requests only. The ignored
local Rust config remains `dry`, uses zero addresses and has every lane
disabled. It is not an input to the documented roster path. Do not treat a
successful process start as evidence that a copy strategy works.

## Candidate protocol

[`protocol.json`](protocol.json) freezes the discovery rule before forward
observations are evaluated:

- take the top five weekly P&L rows from the crypto, sports, politics,
  economics and finance leaderboards;
- observe the union of every returned wallet, including overlapping and
  inactive-looking rows;
- apply no P&L, volume, activity or execution threshold after seeing results;
- label observed trades by their actual event tags, separately from the
  leaderboard categories that supplied the wallet.

The 2026-09-13 19:26 UTC refresh produced 22 unique candidates. The former
manual starting wallets are not selected by this frozen rule. L5ZN was crypto
rank 787 with $832.58 weekly leaderboard P&L, CE25 was rank 87 with $9,515.22,
and `0xb27…b5b82` was rank 30 with $20,049.14. These leaderboard figures are
discovery inputs, not reconstructed or follower P&L.

Refresh the ignored evidence snapshot and observer roster:

```sh
python3 experiment/refresh_candidates.py \
  --snapshot experiment/data/candidate-snapshot.json \
  --roster experiment/data/candidate-roster.json
```

## Forward observer

[`observe_wallets.py`](observe_wallets.py) polls the public v2 trades and
activity feeds every 500 ms. It maintains a separate no-backfill baseline
for each feed and wallet. For each BUY first visible on either feed after that
baseline, it immediately samples the public CLOB book.
Requests run concurrently so wallet position in the roster does not create a
serial detection penalty.

## Pending-transaction observer

`copybot-mempool-observer` subscribes to full Polygon pending transactions over
WebSocket and decodes watched-wallet matches into a separate append-only file.
It imports feed and calldata modules only: there is no signer, custody, CLOB
authentication or order path. The retained service uses one PublicNode socket
and writes `/var/lib/copybot-paper/mempool-observations.jsonl`. This is a
detection lane, not fill or profitability evidence.

Each paper quote:

- walks displayed asks only up to the frozen 0.15-cent price limit relative to
  the leader price;
- includes fee parameters read from that market at sample time;
- assumes no wallet-specific rebate or fee advantage;
- checks the market's minimum order size as well as displayed depth;
- records leaderboard membership and the actual market categories returned by
  the event API.

The same book snapshot also evaluates two pre-declared paper challengers. The
first tests fixed price limits of 0.15, 0.5 and 1 cent above the leader price.
The second raises the paper amount to the market's exact minimum share size,
but only when the resulting cash requirement is no more than $5. The control
quote remains unchanged. Feed detections are recorded separately so later
analysis can compare v2 trades with v2 activity without counting one trade
twice.

SELL signals are retained but not scored because the experiment does not yet
have follower inventory. Public activity timestamps have one-second precision,
and visibility occurs after the leader match. A sampled book is not a fill.

Establish a baseline:

```sh
python3 experiment/observe_wallets.py \
  --roster experiment/data/candidate-roster.json \
  --state experiment/run/candidate-observer-state.json \
  --output experiment/data/candidate-observations.jsonl \
  --max-polls 1
```

Then collect a bounded forward sample:

```sh
python3 experiment/observe_wallets.py \
  --roster experiment/data/candidate-roster.json \
  --state experiment/run/candidate-observer-state.json \
  --output experiment/data/candidate-observations.jsonl \
  --max-polls 18 --poll-seconds 5 --delay-ms 250
```

Summarise the append-only observations:

```sh
python3 experiment/evaluate_observations.py \
  --input experiment/data/candidate-observations.jsonl \
  --output experiment/data/candidate-summary.json
```

The clean bounded verification run on 2026-09-13 captured 50 BUY and nine SELL
signals without an API error. Three BUYs were executable under both the price
cap and minimum-size rule. Public detection lag ranged from 2,398 ms to 42,267
ms (median 21,267 ms). Actual book sampling occurred 337 to 1,739 ms after
detection (median 522 ms). Encountered market fee rates were 0, 0.04, 0.05 and
0.07. This sample proves the collection and quote path, not profitability.

## Resource and hosting decision

Measured locally with `/usr/bin/time -l`:

| Operation | Wall time | Maximum RSS |
| --- | ---: | ---: |
| Refresh 22 candidates | 0.59 s | 49 MB |
| Baseline 22 wallets | 0.38 s | 64 MB |
| 12 polls at 5 s, including 59 signals | 82.23 s | 80 MB |

The retained evidence was 101 KB for this run. The `fast-v1` forward cohort
uses separate state and observation files, so its zero-delay evidence is not
pooled with the earlier 250 ms sample. The live observer now runs on a
dedicated `t4g.micro` in AWS `eu-central-2` (Zurich), with 2 vCPU, 1 GiB RAM and
an encrypted 8 GiB gp3 root volume. The instance is
`i-04b7b5e56196ff512`. It is separate from the existing Zurich research host.
The service has no credentials and deploys only the public Python observer.

AWS's current on-demand compute price is $0.0106 per hour, about $7.74 for a
730-hour month. An assigned public IPv4 address adds $0.005 per hour, about
$3.65 per month. The 8 GiB gp3 volume is additional, so the expected total is
about $12 to $13 per month before tax and outbound data.

## Verification

```sh
python3 -m unittest discover -s experiment -p 'test_*.py'
cargo test --locked --manifest-path hot/Cargo.toml --lib --bin copybot-hot
cargo build --locked --release --manifest-path hot/Cargo.toml --bin copybot-hot
```

Realised or resolved follower outcomes are still absent. Do not define a
promotion threshold or choose wallets until a pre-declared observation window
contains enough settled or independently closeable paper positions.
