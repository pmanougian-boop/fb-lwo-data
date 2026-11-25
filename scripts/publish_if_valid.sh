#!/usr/bin/env bash
# Commit the validated forecast onto main. Refuses every other event and ref,
# so a pull_request or a branch push cannot replace the Pages file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

event="${GITHUB_EVENT_NAME:-}"
ref="${GITHUB_REF:-}"
case "$event" in
    schedule|workflow_dispatch) ;;
    *)
        echo "[lwo-publish] refusing: event '$event' does not publish"
        exit 1
        ;;
esac
if [ "$ref" != "refs/heads/main" ]; then
    echo "[lwo-publish] refusing: ref '$ref' is not main"
    exit 1
fi

src="${LWO_OUT_DIR:-$ROOT/staging}"
forecast="$src/lwo-forecast.json"
status="$src/lwo-status.json"
if [ ! -f "$forecast" ] || [ ! -f "$status" ]; then
    echo "[lwo-publish] refusing: staging forecast or status is missing"
    exit 1
fi

python3 - "$status" << 'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
if not doc.get("ok") or doc.get("validationErrors"):
    raise SystemExit("[lwo-publish] refusing: status is not ok")
ens = doc.get("ens") or {}
if ens.get("src") != "ecmwf-ens":
    raise SystemExit("[lwo-publish] refusing: ECMWF ENS missing from status")
PY

cp -f "$forecast" "$ROOT/lwo-forecast.json"
# Mark the committed status as the published copy. The staging artifact stays unpublished.
python3 - "$status" "$ROOT/lwo-status.json" << 'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
doc["published"] = True
doc["event"] = "schedule" if doc.get("event") == "schedule" else doc.get("event")
json.dump(doc, open(sys.argv[2], "w", encoding="utf-8"), indent=2)
open(sys.argv[2], "a", encoding="utf-8").write("\n")
PY

git add -- lwo-forecast.json lwo-status.json
while IFS= read -r name; do
    [ -z "$name" ] && continue
    case "$name" in
        lwo-forecast.json|lwo-status.json) ;;
        *)
            echo "[lwo-publish] refusing: staged path '$name' is not the forecast or the status"
            git reset -q
            exit 1
            ;;
    esac
done << EOF
$(git diff --cached --name-only)
EOF
if git diff --cached --quiet; then
    echo "[lwo-publish] nothing to commit"
    exit 0
fi

t0=$(python3 -c 'import json; print(json.load(open("lwo-forecast.json"))["t0"])')
gen=$(python3 -c 'import json; print(json.load(open("lwo-forecast.json"))["generatedAt"])')
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git commit -m "Forecast run ${gen} (t0 ${t0})"
git push origin HEAD:main
echo "[lwo-publish] pushed forecast t0 ${t0}"
