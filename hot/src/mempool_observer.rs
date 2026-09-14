use copybot_hot::calldata::decode_all;
use copybot_hot::feeds::{self, FeedConfig, RawTx, WatchedAddresses};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;

const DEFAULT_FEED: &str = "wss://polygon-bor-rpc.publicnode.com";
const SEEN_LIMIT: usize = 20_000;
const CLOB_HOST: &str = "https://clob.polymarket.com";

#[derive(Clone)]
struct Lane {
    name: String,
    wallet: String,
    pct: f64,
    max_usd: f64,
    min_usd: f64,
    min_fill_floor: bool,
    buy_slippage_c: f64,
}

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

fn wallets(roster: &Value) -> Result<HashMap<[u8; 20], Lane>, String> {
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
        result.insert(address, Lane {
            name: name.to_string(),
            wallet: format!("0x{}", hex::encode(address)),
            pct: row["pct"].as_f64().ok_or(format!("lane {name} pct is required"))?,
            max_usd: row["max_usd_per_fill"].as_f64()
                .ok_or(format!("lane {name} max_usd_per_fill is required"))?,
            min_usd: row["min_order_usd"].as_f64()
                .ok_or(format!("lane {name} min_order_usd is required"))?,
            min_fill_floor: row["min_fill_floor"].as_bool().unwrap_or(false),
            buy_slippage_c: row["buy_slippage_c"].as_f64()
                .ok_or(format!("lane {name} buy_slippage_c is required"))?,
        });
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

fn events(raw: &RawTx, roster: &HashMap<[u8; 20], Lane>) -> Vec<(Value, Lane)> {
    let observed_at_ms = raw.seen_ns / 1_000_000;
    let mut result = Vec::new();
    for (address, lane) in roster {
        for decoded in decode_all(&raw.input, address) {
            result.push((json!({
                "schema": "copybot.mempool-observation.v1",
                "kind": "mempool_detection",
                "observed_at_ms": observed_at_ms,
                "observed_at_ns": raw.seen_ns.to_string(),
                "source": raw.source,
                "transaction_hash": raw.hash,
                "lane": lane.name,
                "leader": lane.wallet,
                "condition_id": format!("0x{}", hex::encode(decoded.condition_id)),
                "token_id": decoded.token_id,
                "side": if decoded.side == 0 { "BUY" } else { "SELL" },
                "price": decoded.price,
                "order_size": decoded.order_size,
                "fill_size": decoded.fill_size,
                "role": decoded.role,
                "occurrence": decoded.occurrence,
                "order_capability": false
            }), lane.clone()));
        }
    }
    result
}

fn number(value: &Value) -> Option<f64> {
    value.as_f64().or_else(|| value.as_str()?.parse().ok())
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

fn paper_quote(asks: &[Value], cash: f64, fee_rate: f64, min_shares: f64, max_price: f64) -> Value {
    let mut levels: Vec<(f64, f64)> = asks.iter().filter_map(|row| {
        Some((number(&row["price"])?, number(&row["size"])?))
    }).collect();
    levels.sort_by(|a, b| a.0.total_cmp(&b.0));
    let (mut remaining, mut shares, mut cost, mut fee, mut used) = (cash, 0.0, 0.0, 0.0, 0);
    for (price, available) in levels {
        if price > max_price { break; }
        let unit_total = price + fee_rate * price * (1.0 - price);
        if price <= 0.0 || available <= 0.0 || unit_total <= 0.0 { continue; }
        let take = available.min(remaining / unit_total);
        if take <= 0.0 { break; }
        let level_cost = take * price;
        let level_fee = take * fee_rate * price * (1.0 - price);
        shares += take;
        cost += level_cost;
        fee += level_fee;
        remaining -= level_cost + level_fee;
        used += 1;
        if remaining < 1e-9 { break; }
    }
    let filled = cost + fee >= cash - 1e-6;
    let minimum_size_met = shares >= min_shares;
    json!({
        "requested_cash_usd": cash, "filled": filled,
        "meets_min_order_size": minimum_size_met,
        "executable": filled && minimum_size_met,
        "shares": shares, "notional_usd": cost, "fee_usd": fee,
        "total_usd": cost + fee, "vwap": if shares > 0.0 { Some(cost / shares) } else { None },
        "levels": used, "unspent_usd": remaining.max(0.0), "max_price": max_price
    })
}

async fn sample_book(client: reqwest::Client, output: PathBuf, detection: Value, lane: Lane) {
    if detection["side"] != "BUY" { return; }
    let detected_at_ms = detection["observed_at_ms"].as_u64().unwrap_or(0);
    let token = detection["token_id"].as_str().unwrap_or("");
    let condition = detection["condition_id"].as_str().unwrap_or("");
    let book_url = format!("{CLOB_HOST}/book?token_id={token}");
    let book = match client.get(book_url).send().await.and_then(|r| r.error_for_status()) {
        Ok(response) => match response.json::<Value>().await { Ok(value) => value, Err(error) => {
            let _ = append(&output, &json!({"schema":"copybot.mempool-observation.v2", "kind":"mempool_quote_error", "observed_at_ms":now_ms(), "transaction_hash":detection["transaction_hash"], "lane":lane.name, "error":format!("book decode: {error}"), "order_capability":false})); return;
        }},
        Err(error) => { let _ = append(&output, &json!({"schema":"copybot.mempool-observation.v2", "kind":"mempool_quote_error", "observed_at_ms":now_ms(), "transaction_hash":detection["transaction_hash"], "lane":lane.name, "error":format!("book request: {error}"), "order_capability":false})); return; }
    };
    let sampled_at_ms = now_ms();
    let market_url = format!("{CLOB_HOST}/clob-markets/{condition}");
    let market = match client.get(market_url).send().await.and_then(|r| r.error_for_status()) {
        Ok(response) => response.json::<Value>().await.unwrap_or(Value::Null),
        Err(_) => Value::Null,
    };
    let fee_rate = number(&market["fd"]["r"]).unwrap_or(0.0);
    let minimum = number(&market["mos"]).unwrap_or(0.0);
    let price = detection["price"].as_f64().unwrap_or(0.0);
    let leader_cash = detection["fill_size"].as_f64().unwrap_or(0.0) * price;
    let mut cash = lane.max_usd.min(leader_cash * lane.pct);
    if lane.min_fill_floor && cash > 0.0 && cash < lane.min_usd { cash = lane.min_usd; }
    let quote = paper_quote(book["asks"].as_array().map(Vec::as_slice).unwrap_or(&[]), cash, fee_rate, minimum, (price + lane.buy_slippage_c / 100.0).min(1.0));
    let uncapped_quote = paper_quote(
        book["asks"].as_array().map(Vec::as_slice).unwrap_or(&[]),
        cash, fee_rate, minimum, 1.0,
    );
    let _ = append(&output, &json!({
        "schema":"copybot.mempool-observation.v2", "kind":"mempool_quote",
        "observed_at_ms":sampled_at_ms, "detected_at_ms":detected_at_ms,
        "actual_sample_delay_ms":sampled_at_ms.saturating_sub(detected_at_ms),
        "source":detection["source"], "transaction_hash":detection["transaction_hash"],
        "lane":lane.name, "leader":lane.wallet, "condition_id":condition, "token_id":token,
        "side":"BUY", "leader_price":price, "leader_fill_size":detection["fill_size"],
        "market":{"min_order_size":market["mos"], "tick_size":market["mts"], "fee_rate":fee_rate},
        "book":{"timestamp":book["timestamp"], "hash":book["hash"],
            "best_bid":book["bids"].as_array().and_then(|v| v.iter().filter_map(|r| number(&r["price"])).max_by(f64::total_cmp)),
            "best_ask":book["asks"].as_array().and_then(|v| v.iter().filter_map(|r| number(&r["price"])).min_by(f64::total_cmp))},
        "paper_quote":quote, "paper_challengers":{"uncapped":uncapped_quote},
        "order_capability":false,
        "limitations":["book sampling is not an order or fill", "pending transactions can be replaced or dropped"]
    }));
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
    watched.set(roster.values().map(|lane| lane.wallet.clone()));
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
    let client = reqwest::Client::builder().timeout(Duration::from_secs(5)).build()
        .map_err(|e| format!("build HTTP client: {e}"))?;
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
        for (event, lane) in events(&raw, &roster) {
            append(&args.output, &event)?;
            tokio::spawn(sample_book(client.clone(), args.output.clone(), event, lane));
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
        let value = json!({"lanes": [{"name": "one", "wallet": format!("0x{}", "1".repeat(40)), "pct":0.005, "max_usd_per_fill":2.0, "min_order_usd":1.0, "min_fill_floor":true, "buy_slippage_c":0.15}]});
        assert_eq!(wallets(&value).unwrap().len(), 1);
    }

    #[test]
    fn paper_quote_enforces_depth_price_and_minimum() {
        let quote = paper_quote(&[json!({"price":"0.50","size":"4"}), json!({"price":"0.51","size":"10"})], 2.0, 0.0, 5.0, 0.50);
        assert_eq!(quote["filled"], true);
        assert_eq!(quote["meets_min_order_size"], false);
        assert_eq!(quote["executable"], false);
    }
}
