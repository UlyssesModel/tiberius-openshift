#!/usr/bin/env bash
# 28-cell AMX ISA-ceiling sweep wrapper.
# Runs default-kernel cells first (no sudo), then rt-fifo cells (sudo -E + chrt + taskset).
# Within each kernel, ascending N so fast cells finish first.
set -uo pipefail

SWEEP_DIR=${SWEEP_DIR:-/tmp/sweep}
mkdir -p "$SWEEP_DIR"
SWEEP_CSV="$SWEEP_DIR/sweep.csv"
SUMMARY_CSV="$SWEEP_DIR/sweep_summary.csv"
VERBOSE_LOG="$SWEEP_DIR/onednn_verbose_sample.txt"
HARNESS=${HARNESS:-/tmp/bench_amx_sweep.py}

# Fresh start
rm -f "$SWEEP_CSV" "$SUMMARY_CSV" "$VERBOSE_LOG"

run_cell() {
  local N="$1" isa="$2" kernel="$3"
  local cell_id="N${N}-${isa}-${kernel}"
  local warmup=50
  [[ "$isa" == "amx-off" ]] && warmup=100

  # ISA env
  local env_pairs=()
  [[ "$isa" == "amx-off" ]] && env_pairs+=("ONEDNN_MAX_CPU_ISA=AVX512_CORE")

  # ONEDNN_VERBOSE only for the one chosen cell
  local capture_verbose=0
  if [[ "$N" == "2048" && "$isa" == "amx-on" && "$kernel" == "rt-fifo" ]]; then
    env_pairs+=("ONEDNN_VERBOSE=1")
    capture_verbose=1
  fi

  printf "[%s] cell %-30s ... " "$(date +%H:%M:%S)" "$cell_id"

  local args=(
    --N "$N" --isa "$isa" --kernel "$kernel" --cell-id "$cell_id"
    --samples 10000 --max-seconds 120 --warmup "$warmup"
    --out "$SWEEP_CSV" --summary "$SUMMARY_CSV"
  )

  if [[ "$kernel" == "rt-fifo" ]]; then
    if [[ "$capture_verbose" == "1" ]]; then
      # Capture full stderr (oneDNN verbose) + stdout (bench summary line) to log
      sudo -E env "${env_pairs[@]}" chrt -f 80 taskset -c 4 \
        python3.11 "$HARNESS" "${args[@]}" > "$VERBOSE_LOG" 2>&1
      # Also echo the final summary line to console
      tail -1 "$VERBOSE_LOG"
    else
      sudo -E env "${env_pairs[@]}" chrt -f 80 taskset -c 4 \
        python3.11 "$HARNESS" "${args[@]}"
    fi
  else
    env "${env_pairs[@]}" taskset -c 4 \
      python3.11 "$HARNESS" "${args[@]}"
  fi
}

echo "=== AMX ISA-ceiling sweep — 28 cells ==="
echo "  output: $SWEEP_CSV (per-call), $SUMMARY_CSV (per-cell), $VERBOSE_LOG (one cell)"
echo

# Default kernel first (fast, no sudo)
echo "--- default-kernel cells (taskset -c 4) ---"
for N in 64 128 256 512 1024 2048 4096; do
  for isa in amx-on amx-off; do
    run_cell "$N" "$isa" "default"
  done
done

# RT-FIFO cells (sudo -E + chrt -f 80)
echo
echo "--- rt-fifo cells (sudo -E chrt -f 80 taskset -c 4) ---"
for N in 64 128 256 512 1024 2048 4096; do
  for isa in amx-on amx-off; do
    run_cell "$N" "$isa" "rt-fifo"
  done
done

echo
echo "=== summary ==="
column -t -s, "$SUMMARY_CSV"
echo
echo "amx_bf16 dispatches in verbose log: $(grep -c 'brg_matmul:avx10_1_512_amx' $VERBOSE_LOG 2>/dev/null || echo 0)"
echo "rows in per-call CSV: $(wc -l < $SWEEP_CSV)"
