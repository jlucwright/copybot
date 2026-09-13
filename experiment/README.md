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

[`observe_wallets.py`](observe_wallets.py) polls the public activity API. Its
first poll establishes a no-backfill baseline. For each BUY first visible on a
later poll, it waits 250 ms from detection and then samples the public CLOB
book. Activity requests run concurrently so wallet position in the roster does
not create a serial detection penalty.

Each paper quote:

- walks displayed asks only up to the frozen 0.15-cent price limit relative to
  the leader price;
- includes fee parameters read from that market at sample time;
- assumes no wallet-specific rebate or fee advantage;
- checks the market's minimum order size as well as displayed depth;
- records leaderboard membership and the actual market categories returned by
  the event API.

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

The retained evidence was 101 KB for this run. Allowing for Python, OS services,
log rotation and bursts, one dedicated `t4g.small` in AWS `eu-central-2`
(Zurich), with 2 vCPU, 2 GiB RAM and an 8 GiB gp3 root volume, is sufficient.
Use a separate instance and service account. Do not place this process on the
existing overloaded Zurich research host.

No infrastructure has been created or changed. The remaining hosting choice is
whether to approve that dedicated `t4g.small`. Until then, keep the experiment
local and bounded. Confirm the account's current on-demand price or free-trial
eligibility before provisioning.

## Verification

```sh
python3 -m unittest discover -s experiment -p 'test_*.py'
cargo test --locked --manifest-path hot/Cargo.toml --lib --bin copybot-hot
cargo build --locked --release --manifest-path hot/Cargo.toml --bin copybot-hot
```

Realised or resolved follower outcomes are still absent. Do not define a
promotion threshold or choose wallets until a pre-declared observation window
contains enough settled or independently closeable paper positions.
