# Paper wallet-copy experiment

Evidence cut: 2026-09-13, public Polymarket activity and user-P&L APIs.

This is a separate local checkout of the upstream copybot. It is an experiment,
not a production bot. The only enabled lanes in the local dry-run config are:

| Lane | Address | Reason | Current state |
| --- | --- | --- | --- |
| `l5zn_btc15` | `0xb945945d5bcaf7b56834d4da8cdf8f8f94b2db68` | Large current sample, +$1,090.21 user-P&L over the latest day, but maker execution remains unproven. | Enabled, dry only |
| `ce25_eth15` | `0xce25e214d5cfe4f459cf67f08df581885aae7fdc` | Current activity and long-run positive reconstruction, with substantial two-sided/pair behaviour. | Enabled, dry only |
| `maker_b27bc9` | `0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82` | Active SOL/XRP maker population, but the paper cell is still small and maker registration is not queue proof. | Disabled |

The local config uses zero addresses, absolute paper caps, no feed, and no
private key. It must not be changed to `shadow` or `live` as part of this
experiment. A feed and measured lane statistics are required before any
future replay can be considered, and the public activity feed cannot prove
that a follower receives the leader's maker fill.

Fee policy is deliberately conservative: paper scoring charges the full
market taker fee and never borrows a leader's rebate tier. Polymarket applies
fees at match time, and any taker rebate belongs to the executing follower's
own tier. For the observed crypto markets the current fee detail is `r=0.07`,
so a two-share $0.50 fill would carry a $0.035 fee before any independently
verified follower rebate. Makers are fee-free, but the disabled maker lane has
not earned a registration or queue-proof exception.

Not selected: Bonereaper (historical reconstruction gap and two-sided flow),
DoggyStyIe (public-feed follower timing was falsified), WQEWQA, Goingdown and
9F5 (directional or negative current evidence), pbot6 (recent spike without
the required longer-run validation), a689 and `me` (insufficient sample), and
polmaxi (no activity in the latest day despite a populated profile).

Non-crypto leaderboard wallets remain discovery-only. A one-day check found
that a high weekly leaderboard number can be stale or extremely concentrated
(for example, the sampled sports leader had two trades), so none is wired into
the copy lanes yet.

Run the parser/build check from the repository root:

```sh
cargo test --locked --manifest-path hot/Cargo.toml --lib --bin copybot-hot
cargo build --locked --release --manifest-path hot/Cargo.toml --bin copybot-hot
```

## Public paper observer

The Rust transaction feed is not used for this experiment because leader fills
are matched off-chain before settlement. `observe_wallets.py` polls the official
public activity endpoint, suppresses everything present at its first baseline,
and samples the public CLOB book only for trades first observed afterwards.
It neither imports nor accepts a private key.

Run a single baseline poll:

```sh
python3 experiment/observe_wallets.py \
  --config deploy/copybot2.paper.toml \
  --state experiment/run/wallet-observer-state.json \
  --output experiment/data/wallet-observations.jsonl
```

For a bounded forward sample, add `--max-polls 30 --poll-seconds 10`. The
observer honours bounded rate-limit retries. Each BUY
quote walks the book after a 250 ms follower delay, caps total spend at the
lane's paper limit, and includes the market-specific taker fee. SELL signals
are retained but not scored because the follower inventory is not yet tracked.
This evidence tests observability and executable entry prices, not profitability.
The local config also gives each enabled lane an explicit `title_contains`
filter, preventing a wallet's trades in another market family from entering the
wrong cohort.
