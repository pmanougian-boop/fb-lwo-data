#!/usr/bin/env bash
# Run the vendored LWO engine and validate the JSON. Writes staging files only.
# Never commits and never touches lwo-forecast.json.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${LWO_PYTHON:-python3}"
OUT_DIR="${LWO_OUT_DIR:-$ROOT/staging}"
PREV="${LWO_PREV:-$ROOT/lwo-forecast.json}"
EVENT="${LWO_EVENT:-${GITHUB_EVENT_NAME:-local}}"
mkdir -p "$OUT_DIR/out"
OUT="$OUT_DIR/out/lwo-forecast.json"
LOG="$OUT_DIR/lwo-run.log"
STATUS="$OUT_DIR/lwo-status.json"
DIFF="$OUT_DIR/diff-summary.json"
PUBLISHED="$OUT_DIR/lwo-forecast.json"

export PYTHONDONTWRITEBYTECODE=1
echo "[lwo-job] python $($PY -c 'import sys; print(sys.version.split()[0])')"
echo "[lwo-job] disk before run:"
df -h "$HOME" "$OUT_DIR" 2>/dev/null || df -h .

started=$(date +%s)
set +e
"$PY" "$ROOT/engine/lwo_w3.py" \
    --config "$ROOT/engine/sites.json" \
    --out "$OUT" \
    --prev "$PREV" \
    --allow-low-min-free \
    >"$LOG" 2>&1
code=$?
set -e
finished=$(date +%s)
echo "[lwo-job] engine exit $code after $((finished - started)) s"
# The artifact path is staging/lwo-forecast.json. Copy only after a write.
if [ -f "$OUT" ]; then
    cp -f "$OUT" "$PUBLISHED"
fi
set +e
"$PY" "$ROOT/engine/validate_forecast.py" \
    --forecast "$PUBLISHED" \
    --prev "$PREV" \
    --log "$LOG" \
    --status "$STATUS" \
    --diff "$DIFF" \
    --engine-exit "$code" \
    --started-epoch "$started" \
    --finished-epoch "$finished" \
    --event "$EVENT" \
    --require-fresh
vcode=$?
set -e
echo "[lwo-job] status:"
cat "$STATUS"
exit "$vcode"
