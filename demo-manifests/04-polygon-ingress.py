#!/usr/bin/env python3
"""
Polygon → AMQ Streams ingress producer for the Ulysses demo.

Two modes, selected via the MODE environment variable:
  * live   — subscribes to Polygon WebSocket, re-emits to Kafka
  * replay — reads Polygon flat-file historical trades for a date, re-emits
             to Kafka at wall-clock pacing (or faster, for shorter demos)

Environment variables expected (from polygon-ingress-config ConfigMap):
  POLYGON_API_KEY         (from Secret)
  MODE                    live | replay
  REPLAY_DATE             YYYY-MM-DD (replay only)
  TICKERS                 comma-separated list, or "*" for all
  KAFKA_BOOTSTRAP         bootstrap servers
  TOPIC_TRADES, TOPIC_QUOTES
  SECURITY_PROTOCOL       SSL

Exposes Prometheus metrics on :9100/metrics (rate per topic, lag, errors).
"""
import datetime
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

# Third-party deps — install via the ubi9/python-311 base or a custom image:
#   pip install confluent-kafka websocket-client requests prometheus-client
from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, start_http_server
import requests
import websocket

log = logging.getLogger("polygon-ingress")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# ---------- metrics ----------
M_MSGS = Counter("polygon_messages_total", "Messages emitted", ["topic"])
M_ERR = Counter("polygon_errors_total", "Errors", ["kind"])
M_LAG = Gauge("polygon_ingress_lag_seconds", "Wall-clock ingress lag")
M_MODE = Gauge("polygon_mode", "1=live, 2=replay")
M_OPT_SUBS = Gauge(
    "polygon_options_subscriptions_count",
    "Active OCC contracts subscribed on the options websocket",
)
M_OPT_CHAIN_REFRESH = Counter(
    "polygon_options_chain_refresh_total",
    "Options chain REST refresh attempts",
    ["status"],
)


# ---------- kafka ----------
def make_producer() -> Producer:
    return Producer(
        {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP"],
            "security.protocol": os.environ.get("SECURITY_PROTOCOL", "SSL"),
            "ssl.ca.location": "/etc/kafka-certs/ca.crt",
            "ssl.certificate.location": "/etc/kafka-certs/user.crt",
            "ssl.key.location": "/etc/kafka-certs/user.key",
            "linger.ms": 5,
            "compression.type": "snappy",
            "acks": "all",
            "enable.idempotence": True,
            "client.id": "polygon-ingress",
        }
    )


def delivery_cb(err, msg):
    if err is not None:
        M_ERR.labels("delivery").inc()
        log.error("delivery failed: %s", err)
    else:
        M_MSGS.labels(msg.topic()).inc()


def normalize(ev: dict) -> tuple[str, bytes, bytes]:
    """Polygon emits arrays of events. For each, produce (topic, key, value).

    Options OCC symbols are prefixed "O:" by Polygon; equities are bare. Stay
    pure/stateless — options/equities split is a string check, not config."""
    ev_type = ev.get("ev")
    sym = ev.get("sym", "")
    is_option = sym.startswith("O:")
    if ev_type == "T":  # trade
        topic = os.environ["TOPIC_OPTIONS_TRADES"] if is_option else os.environ["TOPIC_TRADES"]
    elif ev_type == "Q":  # NBBO quote
        topic = os.environ["TOPIC_OPTIONS_QUOTES"] if is_option else os.environ["TOPIC_QUOTES"]
    else:
        return None, None, None
    key = sym.encode("utf-8")
    value = json.dumps(ev, separators=(",", ":")).encode("utf-8")
    return topic, key, value


# ---------- live mode ----------
def run_live(producer: Producer, tickers: Iterable[str]):
    M_MODE.set(1)
    api_key = os.environ["POLYGON_API_KEY"]
    url = "wss://socket.polygon.io/stocks"

    def on_open(ws):
        log.info("connected to polygon websocket")
        ws.send(json.dumps({"action": "auth", "params": api_key}))
        subs = []
        for t in tickers:
            subs.append(f"T.{t}")
            subs.append(f"Q.{t}")
        ws.send(json.dumps({"action": "subscribe", "params": ",".join(subs)}))
        Path("/tmp/ingress-ready").touch()

    def on_message(ws, message):
        try:
            events = json.loads(message)
            for ev in events:
                topic, key, value = normalize(ev)
                if topic:
                    producer.produce(topic, key=key, value=value, callback=delivery_cb)
            producer.poll(0)
            Path("/tmp/ingress-healthy").touch()
        except Exception as e:
            M_ERR.labels("normalize").inc()
            log.exception("on_message failed: %s", e)

    def on_error(ws, err):
        M_ERR.labels("ws").inc()
        log.error("websocket error: %s", err)

    def on_close(ws, code, reason):
        log.warning("ws closed (%s): %s", code, reason)

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)


# ---------- options chain enumeration ----------
def enumerate_options_chain(underlying: str, dte_max: int) -> list[str]:
    """Return sorted, deduped OCC tickers ("O:SPY...") for `underlying` whose
    expiration is within `dte_max` calendar days from today.

    Window is queried as today..today+5 (5 not dte_max+1 to absorb weekends
    and holidays where Polygon's Friday expiry counts as +3 not +1) then
    filtered locally to <= dte_max DTE."""
    api_key = os.environ["POLYGON_API_KEY"]
    today = datetime.date.today()
    end = today + datetime.timedelta(days=5)
    base = "https://api.polygon.io/v3/reference/options/contracts"
    params = {
        "underlying_ticker": underlying,
        "expiration_date.gte": today.isoformat(),
        "expiration_date.lte": end.isoformat(),
        "expired": "false",
        "limit": 1000,
        "apiKey": api_key,
    }
    seen: set[str] = set()
    url = base
    next_params = params
    while True:
        r = requests.get(url, params=next_params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for c in data.get("results", []):
            exp = c.get("expiration_date")
            if not exp:
                continue
            try:
                exp_date = datetime.date.fromisoformat(exp)
            except ValueError:
                continue
            if (exp_date - today).days > dte_max:
                continue
            t = c.get("ticker")
            if t:
                seen.add(t)
        next_url = data.get("next_url")
        if not next_url:
            break
        # next_url already encodes the cursor; re-attach apiKey only.
        url = next_url
        next_params = {"apiKey": api_key}
    return sorted(seen)


# ---------- live mode (options) ----------
def run_live_options(producer: Producer, underlyings: Iterable[str], dte_max: int):
    """Parallel WebSocket to /stocks: subscribes to T.O:<OCC>+Q.O:<OCC> for the
    full 0..dte_max chain of each underlying. Refreshes the chain every 30
    minutes — adds newly listed contracts, drops expired ones."""
    api_key = os.environ["POLYGON_API_KEY"]
    url = "wss://socket.polygon.io/options"
    underlyings = list(underlyings)

    state: dict = {"ws": None, "subs": set()}
    state_lock = threading.Lock()

    def build_subs(symbols: Iterable[str]) -> list[str]:
        out = []
        for s in symbols:
            out.append(f"T.{s}")
            out.append(f"Q.{s}")
        return out

    def refresh_chain() -> set:
        try:
            new_syms: set[str] = set()
            for u in underlyings:
                new_syms.update(enumerate_options_chain(u, dte_max))
            M_OPT_CHAIN_REFRESH.labels("ok").inc()
            return new_syms
        except Exception as e:
            M_OPT_CHAIN_REFRESH.labels("error").inc()
            M_ERR.labels("chain-fetch").inc()
            log.exception("options chain refresh failed: %s", e)
            return None

    def apply_chain(new_syms: set):
        with state_lock:
            ws = state["ws"]
            old = state["subs"]
            added = new_syms - old
            removed = old - new_syms
            if ws is not None and added:
                ws.send(json.dumps({"action": "subscribe",
                                    "params": ",".join(build_subs(added))}))
            if ws is not None and removed:
                ws.send(json.dumps({"action": "unsubscribe",
                                    "params": ",".join(build_subs(removed))}))
            state["subs"] = new_syms
            M_OPT_SUBS.set(len(new_syms))

    def chain_refresher():
        while True:
            time.sleep(30 * 60)
            new_syms = refresh_chain()
            if new_syms is not None:
                apply_chain(new_syms)

    def on_open(ws):
        log.info("connected to polygon options websocket")
        ws.send(json.dumps({"action": "auth", "params": api_key}))
        new_syms = refresh_chain()
        if new_syms is None:
            new_syms = set()
        with state_lock:
            state["ws"] = ws
        # Use apply_chain so adds go out the wire and the gauge updates.
        apply_chain(new_syms)

    def on_message(ws, message):
        try:
            events = json.loads(message)
            for ev in events:
                topic, key, value = normalize(ev)
                if topic:
                    producer.produce(topic, key=key, value=value, callback=delivery_cb)
            producer.poll(0)
        except Exception as e:
            M_ERR.labels("normalize-options").inc()
            log.exception("options on_message failed: %s", e)

    def on_error(ws, err):
        M_ERR.labels("ws-options").inc()
        log.error("options websocket error: %s", err)

    def on_close(ws, code, reason):
        log.warning("options ws closed (%s): %s", code, reason)
        with state_lock:
            state["ws"] = None
            state["subs"] = set()  # force full re-subscribe on next on_open

    threading.Thread(target=chain_refresher, daemon=True, name="options-chain").start()

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)


# ---------- replay mode ----------
def run_replay(producer: Producer, tickers: Iterable[str], date: str):
    """Download Polygon flat files for the given date and replay events at
    wall-clock pacing (so regime-change demos line up with real timestamps)."""
    M_MODE.set(2)
    api_key = os.environ["POLYGON_API_KEY"]
    # Polygon historical trades REST endpoint — aggregated per-ticker.
    # For production replay, pull the flat-file from S3 instead.
    log.info("replay mode: date=%s tickers=%s", date, ",".join(tickers))
    Path("/tmp/ingress-ready").touch()
    for sym in tickers:
        url = (
            f"https://api.polygon.io/v3/trades/{sym}"
            f"?timestamp.gte={date}T13:30:00Z"
            f"&timestamp.lte={date}T20:00:00Z"
            f"&limit=50000&apiKey={api_key}"
        )
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json().get("results", [])
        except Exception as e:
            M_ERR.labels("replay-fetch").inc()
            log.exception("replay fetch failed for %s: %s", sym, e)
            continue
        last_ts = None
        for tr in data:
            ev = {"ev": "T", "sym": sym, "p": tr.get("price"), "s": tr.get("size"),
                  "t": tr.get("sip_timestamp")}
            topic, key, value = normalize(ev)
            if topic is None:
                continue
            # Pace by original timestamp gaps for a realistic demo cadence.
            ts = tr.get("sip_timestamp")
            if last_ts is not None and ts is not None:
                delta = (ts - last_ts) / 1e9
                if 0 < delta < 5:
                    time.sleep(delta)
            last_ts = ts
            producer.produce(topic, key=key, value=value, callback=delivery_cb)
            producer.poll(0)
            Path("/tmp/ingress-healthy").touch()
    log.info("replay complete")


# ---------- main ----------
def main():
    start_http_server(9100)
    producer = make_producer()
    mode = os.environ.get("MODE", "live").lower()
    tickers_env = os.environ.get("TICKERS", "SPY,QQQ")
    tickers = [t.strip() for t in tickers_env.split(",") if t.strip()]
    log.info("starting in mode=%s with %d tickers", mode, len(tickers))

    def shutdown(signum, frame):
        log.info("shutdown signal %s", signum)
        producer.flush(10)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if mode == "replay":
        date = os.environ.get("REPLAY_DATE", "2024-08-05")
        # Options replay is V2 — live-only for now. Equities replay is unchanged.
        run_replay(producer, tickers, date)
        # After replay finishes, idle so the pod stays healthy for another
        # replay run (or live-mode switch via ConfigMap).
        while True:
            time.sleep(60)
            Path("/tmp/ingress-healthy").touch()
    else:
        underlyings_env = os.environ.get("OPTIONS_UNDERLYINGS", "")
        underlyings = [u.strip() for u in underlyings_env.split(",") if u.strip()]
        dte_max = int(os.environ.get("OPTIONS_DTE_MAX", "3"))
        if underlyings:
            log.info("launching options ws thread: underlyings=%s dte_max=%d",
                     ",".join(underlyings), dte_max)
            threading.Thread(
                target=run_live_options,
                args=(producer, underlyings, dte_max),
                daemon=True,
                name="options-ws",
            ).start()
        run_live(producer, tickers)


if __name__ == "__main__":
    main()
