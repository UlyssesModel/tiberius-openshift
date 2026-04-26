#!/usr/bin/env python3
"""
Ulysses SOR demo consumer.

Pipeline shape:

    market.equities.trades  --[confluent-kafka]-->  per-ticker rolling window
                                                           │
                              every WINDOW_SECONDS, for    │
                              each ticker with enough      ▼
                              samples, compute the       density matrix ρ
                              von Neumann entropy of           │
                              a delay-coordinate                ▼
                              density matrix:           S(ρ) = -Tr(ρ ln ρ)
                                                              │
                                                              ▼
                                       ulysses.entropy.equities  (Kafka topic)

THIS IS A STUB. The real Ulysses pipeline uses a normalized energy-based
model (Joel's partition-function breakthrough). That code is air-gapped per
Kavara policy and lives in `SECRET_ulysses*` / `super_kirk*` files off the
scotty-gpu vault. What you're reading is a public-safe demonstration that
exercises the same deployment shape with a textbook density-matrix entropy
calculation. Jarett (or whoever ships production) swaps `compute_entropy`
for the real pipeline and the rest of this code is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Deque, Dict, Optional

import numpy as np
from confluent_kafka import Consumer, Producer, KafkaError
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from pythonjsonlogger import jsonlogger


# ---------- logging ----------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )
)
logging.basicConfig(level=LOG_LEVEL, handlers=[_handler])
log = logging.getLogger("ulysses-sor")


# ---------- config ----------
CFG = {
    "bootstrap": os.environ["KAFKA_BOOTSTRAP"],
    "group_id": os.environ.get("KAFKA_CONSUMER_GROUP", "ulysses-sor-eqty"),
    "topic_in_trades": os.environ.get("TOPIC_TRADES_IN", "market.equities.trades"),
    "topic_in_quotes": os.environ.get("TOPIC_QUOTES_IN", "market.equities.quotes"),
    "topic_out": os.environ.get("TOPIC_ENTROPY_OUT", "ulysses.entropy.equities"),
    "security_protocol": os.environ.get("SECURITY_PROTOCOL", "SSL"),
    "window_seconds": float(os.environ.get("WINDOW_SECONDS", "12")),
    "embed_dim": int(os.environ.get("EMBED_DIM", "32")),
    "min_samples": int(os.environ.get("MIN_SAMPLES", "64")),
    "max_samples": int(os.environ.get("MAX_SAMPLES", "512")),
}


# ---------- metrics ----------
REGISTRY = CollectorRegistry()
M_CONSUMED = Counter(
    "ulysses_messages_consumed_total",
    "Messages consumed from input topics",
    ["topic"],
    registry=REGISTRY,
)
M_PRODUCED = Counter(
    "ulysses_entropy_messages_produced_total",
    "Entropy messages produced",
    registry=REGISTRY,
)
M_WINDOW_DUR = Histogram(
    "ulysses_window_compute_seconds",
    "Time to compute entropy for one (ticker, window) tuple",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)
M_ENTROPY = Gauge(
    "ulysses_entropy_score",
    "Most recent entropy score per ticker",
    ["ticker"],
    registry=REGISTRY,
)
M_WINDOW_SAMPLES = Gauge(
    "ulysses_window_samples",
    "Samples in the current window for each ticker",
    ["ticker"],
    registry=REGISTRY,
)
M_HW = Gauge(
    "ulysses_hardware_id_info",
    "Which hardware_id this pod booted on (1 = active)",
    ["hardware_id"],
    registry=REGISTRY,
)


# ---------- hardware detection (simplified) ----------
def detect_hardware_id() -> str:
    """In production this reads NFD-exposed CPUID labels. For the demo we
    peek /proc/cpuinfo and look for AMX + TDX markers."""
    try:
        info = Path("/proc/cpuinfo").read_text()
    except OSError:
        return "unknown"
    flags = ""
    for line in info.splitlines():
        if line.startswith("flags") and ":" in line:
            flags = line.split(":", 1)[1]
            break
    has_amx = "amx_bf16" in flags or "amx_tile" in flags
    has_avx512 = "avx512f" in flags
    has_tdx = Path("/sys/kernel/tdx").exists() or "tdx_guest" in info
    if has_tdx and has_amx:
        return "gnr-tdx"
    if has_amx:
        return "spr" if "avx10" not in flags else "gnr-baremetal"
    if "AuthenticAMD" in info:
        return "amd-sevsnp" if has_tdx else "amd"
    if has_avx512:
        return "intel-legacy"
    return "unknown"


# ---------- von Neumann entropy of a density matrix ----------
def compute_entropy(returns: np.ndarray, embed_dim: int) -> Optional[float]:
    """Delay-coordinate density matrix entropy.

    Steps:
      1. Take a windowed log-return series r of length N.
      2. Form the delay-coordinate embedding X (shape (N-D+1, D)).
      3. Compute the Gram matrix G = X^T X (shape (D, D)). This is a
         Hermitian, positive-semidefinite matrix.
      4. Normalize to trace 1:  rho = G / tr(G). This IS a density matrix.
      5. Diagonalize to get eigenvalues {lambda_i}.
      6. Von Neumann entropy: S(rho) = -Tr(rho ln rho) = -sum(l_i ln l_i)
         over the (non-zero) eigenvalues. That reduces to Shannon entropy
         of the eigenvalue spectrum after diagonalization.

    Returns None if there aren't enough samples to form a meaningful matrix.
    """
    if returns.size < embed_dim + 4:
        return None
    r = returns - returns.mean()
    # Delay-coordinate embedding.
    X = np.lib.stride_tricks.sliding_window_view(r, embed_dim).copy()
    if X.shape[0] < 2:
        return None
    # Gram matrix.
    G = X.T @ X
    tr = float(np.trace(G))
    if tr <= 1e-12:
        return 0.0
    rho = G / tr
    # Hermitian by construction; eigvalsh is numerically stable.
    eigs = np.linalg.eigvalsh(rho)
    eigs = eigs[eigs > 1e-12]
    if eigs.size == 0:
        return 0.0
    return float(-np.sum(eigs * np.log(eigs)))


# ---------- rolling per-ticker window ----------
class TickerState:
    __slots__ = ("prices", "timestamps", "last_emit")

    def __init__(self, max_samples: int):
        self.prices: Deque[float] = deque(maxlen=max_samples)
        self.timestamps: Deque[int] = deque(maxlen=max_samples)
        self.last_emit: float = 0.0


class EntropyEngine:
    def __init__(self, producer: Producer, topic_out: str, hardware_id: str):
        self.producer = producer
        self.topic_out = topic_out
        self.hardware_id = hardware_id
        self.state: Dict[str, TickerState] = defaultdict(
            lambda: TickerState(CFG["max_samples"])
        )
        self._lock = threading.Lock()

    def ingest(self, sym: str, price: float, ts_ns: int) -> None:
        with self._lock:
            s = self.state[sym]
            s.prices.append(price)
            s.timestamps.append(ts_ns)
            M_WINDOW_SAMPLES.labels(ticker=sym).set(len(s.prices))

    def emit_if_due(self, now: float) -> None:
        with self._lock:
            for sym, s in list(self.state.items()):
                if now - s.last_emit < CFG["window_seconds"]:
                    continue
                if len(s.prices) < CFG["min_samples"]:
                    continue
                prices = np.fromiter(s.prices, dtype=np.float64)
                # log-returns
                if prices.size < 2:
                    continue
                returns = np.diff(np.log(np.maximum(prices, 1e-12)))
                with M_WINDOW_DUR.time():
                    entropy = compute_entropy(returns, CFG["embed_dim"])
                if entropy is None:
                    continue
                payload = {
                    "ticker": sym,
                    "ts": int(now * 1e9),
                    "window_seconds": CFG["window_seconds"],
                    "samples": int(prices.size),
                    "embed_dim": CFG["embed_dim"],
                    "entropy": entropy,
                    "hardware_id": self.hardware_id,
                }
                try:
                    self.producer.produce(
                        self.topic_out,
                        key=sym.encode("utf-8"),
                        value=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    )
                    self.producer.poll(0)
                    M_PRODUCED.inc()
                    M_ENTROPY.labels(ticker=sym).set(entropy)
                except Exception:
                    log.exception("produce failed for %s", sym)
                s.last_emit = now


# ---------- kafka clients ----------
def make_consumer() -> Consumer:
    cfg = {
        "bootstrap.servers": CFG["bootstrap"],
        "group.id": CFG["group_id"],
        "enable.auto.commit": True,
        "auto.commit.interval.ms": 5000,
        "auto.offset.reset": "latest",
        "max.poll.interval.ms": 300000,
        "fetch.min.bytes": 1,
        "fetch.wait.max.ms": 50,
        "session.timeout.ms": 30000,
        "client.id": f"ulysses-sor-{os.environ.get('HOSTNAME', 'local')}",
    }
    if CFG["security_protocol"] == "SSL":
        cfg.update(
            {
                "security.protocol": "SSL",
                "ssl.ca.location": "/etc/kafka-certs/ca.crt",
                "ssl.certificate.location": "/etc/kafka-certs/user.crt",
                "ssl.key.location": "/etc/kafka-certs/user.key",
            }
        )
    c = Consumer(cfg)
    c.subscribe([CFG["topic_in_trades"]])
    return c


def make_producer() -> Producer:
    cfg = {
        "bootstrap.servers": CFG["bootstrap"],
        "linger.ms": 5,
        "compression.type": "lz4",
        "acks": "all",
        "enable.idempotence": True,
        "client.id": f"ulysses-sor-prod-{os.environ.get('HOSTNAME', 'local')}",
    }
    if CFG["security_protocol"] == "SSL":
        cfg.update(
            {
                "security.protocol": "SSL",
                "ssl.ca.location": "/etc/kafka-certs/ca.crt",
                "ssl.certificate.location": "/etc/kafka-certs/user.crt",
                "ssl.key.location": "/etc/kafka-certs/user.key",
            }
        )
    return Producer(cfg)


# ---------- http endpoints ----------
class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # silence default stdout logger
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            body = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/healthz", "/ready"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()


def run_http(port: int = 9100) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("metrics/health server listening on %d", port)
    server.serve_forever()


# ---------- main loop ----------
STOP = threading.Event()


def shutdown(signum, _frame) -> None:
    log.info("shutdown signal %s received", signum)
    STOP.set()


def main() -> int:
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    hardware_id = detect_hardware_id()
    M_HW.labels(hardware_id=hardware_id).set(1)
    log.info("ulysses-sor starting", extra={"hardware_id": hardware_id, "config": CFG})

    threading.Thread(target=run_http, daemon=True).start()

    consumer = make_consumer()
    producer = make_producer()
    engine = EntropyEngine(producer, CFG["topic_out"], hardware_id)

    last_emit_check = time.time()
    try:
        while not STOP.is_set():
            msg = consumer.poll(timeout=0.5)
            if msg is None:
                # Periodic entropy emission even when no messages arrive.
                now = time.time()
                if now - last_emit_check > 1.0:
                    engine.emit_if_due(now)
                    last_emit_check = now
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("kafka error: %s", msg.error())
                continue
            M_CONSUMED.labels(topic=msg.topic()).inc()
            try:
                ev = json.loads(msg.value())
            except Exception:
                log.exception("bad payload")
                continue
            # Polygon trade shape: {"ev":"T","sym":"SPY","p":123.45,"s":100,"t":ts_ns}
            sym = ev.get("sym")
            price = ev.get("p")
            ts_ns = ev.get("t", int(time.time() * 1e9))
            if not sym or price is None:
                continue
            try:
                engine.ingest(sym, float(price), int(ts_ns))
            except Exception:
                log.exception("ingest failed")
                continue
            now = time.time()
            if now - last_emit_check > 1.0:
                engine.emit_if_due(now)
                last_emit_check = now
    finally:
        log.info("shutting down, flushing producer")
        producer.flush(10)
        consumer.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
