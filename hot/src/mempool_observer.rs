use copybot_hot::calldata::decode_all;
use copybot_hot::feeds::{self, FeedConfig, RawTx, WatchedAddresses};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

const DEFAULT_FEED: &str = "wss://polygon-bor-rpc.publicnode.com";
const SEEN_LIMIT: usize = 20_000;

struct Args {
    roster: PathBuf,
    output: PathBuf,
    feed: String,
    max_seconds: u64,
}

fn args() -> Result<Args, String> {
    let mut values = std::env::args().skip(1);
    let mut roster = None;
    let mut output = None;
    let mut feed = DEFAULT_FEED.to_string();
    let mut max_seconds = 0;
    while let Some(flag) = values.next() {
        let value = values
            .next()
            .ok_or_else(|| format!("missing value for {flag}"))?;
        match flag.as_str() {
            "--roster" => roster = Some(PathBuf::from(value)),
            "--output" => output = Some(PathBuf::from(value)),
            "--feed" => feed = value,
            "--max-seconds" => max_seconds = value.parse().map_err(|_| "invalid max-seconds")?,
            _ => return Err(format!("unknown argument: {flag}")),
        }
    }
    if !feed.starts_with("wss://") {
        return Err("feed must use wss://".into());
    }
    Ok(Args {
        roster: roster.ok_or("--roster is required")?,
        output: output.ok_or("--output is required")?,
        feed,
        max_seconds,
    })
}

fn wallets(roster: &Value) -> Result<HashMap<[u8; 20], (String, String)>, String> {
    let rows = roster["lanes"]
        .as_array()
        .ok_or("roster lanes must be an array")?;
    let mut result = HashMap::new();
    for row in rows {
        let name = row["name"].as_str().ok_or("lane name is required")?;
        let wallet = row["wallet"].as_str().ok_or("lane wallet is required")?;
        let raw = hex::decode(wallet.trim_start_matches("0x"))
            .map_err(|_| format!("invalid wallet for {name}"))?;
        let address: [u8; 20] = raw
            .try_into()
            .map_err(|_| format!("invalid wallet for {name}"))?;
        result.insert(
            address,
            (name.to_string(), format!("0x{}", hex::encode(address))),
        );
    }
    if result.is_empty() {
        return Err("roster has no wallets".into());
    }
    Ok(result)
}

fn append(path: &PathBuf, value: &Value) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("open output: {e}"))?;
    let mut encoded = serde_json::to_vec(value).map_err(|e| format!("encode: {e}"))?;
    encoded.push(b'\n');
    file.write_all(&encoded)
        .map_err(|e| format!("write: {e}"))?;
    file.flush().map_err(|e| format!("flush: {e}"))
}

fn events(raw: &RawTx, roster: &HashMap<[u8; 20], (String, String)>) -> Vec<Value> {
    let observed_at_ms = raw.seen_ns / 1_000_000;
    let mut result = Vec::new();
    for (address, (lane, wallet)) in roster {
        for decoded in decode_all(&raw.input, address) {
            result.push(json!({
                "schema": "copybot.mempool-observation.v1",
                "kind": "mempool_detection",
                "observed_at_ms": observed_at_ms,
                "observed_at_ns": raw.seen_ns.to_string(),
                "source": raw.source,
                "transaction_hash": raw.hash,
                "lane": lane,
                "leader": wallet,
                "condition_id": format!("0x{}", hex::encode(decoded.condition_id)),
                "token_id": decoded.token_id,
                "side": if decoded.side == 0 { "BUY" } else { "SELL" },
                "price": decoded.price,
                "order_size": decoded.order_size,
                "fill_size": decoded.fill_size,
                "role": decoded.role,
                "occurrence": decoded.occurrence,
                "order_capability": false
            }));
        }
    }
    result
}

#[tokio::main]
async fn main() -> Result<(), String> {
    let args = args()?;
    let roster_value: Value = serde_json::from_slice(
        &std::fs::read(&args.roster).map_err(|e| format!("read roster: {e}"))?,
    )
    .map_err(|e| format!("parse roster: {e}"))?;
    let roster = wallets(&roster_value)?;
    let watched = Arc::new(WatchedAddresses::new());
    watched.set(roster.values().map(|(_, wallet)| wallet.clone()));
    let stats = Arc::new(feeds::FeedStats::default());
    let (tx, mut rx) = mpsc::unbounded_channel();
    let (_handles, _registry) = feeds::spawn_all(
        &[FeedConfig {
            name: "publicnode".into(),
            url: args.feed,
            sockets: 1,
        }],
        tx,
        stats.clone(),
        watched,
    );
    let deadline = (args.max_seconds > 0)
        .then(|| tokio::time::Instant::now() + Duration::from_secs(args.max_seconds));
    let mut seen = HashSet::new();
    let mut order = VecDeque::new();
    loop {
        let next = if let Some(deadline) = deadline {
            match tokio::time::timeout_at(deadline, rx.recv()).await {
                Ok(value) => value,
                Err(_) => break,
            }
        } else {
            rx.recv().await
        };
        let Some(raw) = next else { break };
        if !seen.insert(raw.hash.clone()) {
            continue;
        }
        order.push_back(raw.hash.clone());
        if order.len() > SEEN_LIMIT {
            if let Some(old) = order.pop_front() {
                seen.remove(&old);
            }
        }
        for event in events(&raw, &roster) {
            append(&args.output, &event)?;
        }
    }
    eprintln!(
        "frames={} exchange_txs={} errors={}",
        stats.frames.load(std::sync::atomic::Ordering::Relaxed),
        stats
            .exchange_txs
            .load(std::sync::atomic::Ordering::Relaxed),
        stats.errors.load(std::sync::atomic::Ordering::Relaxed),
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roster_rejects_missing_wallets() {
        assert!(wallets(&json!({"lanes": []})).is_err());
    }

    #[test]
    fn roster_accepts_valid_wallet() {
        let value = json!({"lanes": [{"name": "one", "wallet": format!("0x{}", "1".repeat(40))}]});
        assert_eq!(wallets(&value).unwrap().len(), 1);
    }
}
