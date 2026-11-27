#!/usr/bin/env bash
# Write ok:true or ok:false on the Actions summary, and keep a single issue
# for a failed run that was supposed to publish. Exits 0: this step must not
# be able to fail the refresh. It does not send mail.
#
#   report_refresh.sh <success|failure> <none|open|close>
set -u
outcome="${1:-failure}"
action="${2:-none}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
status="$ROOT/staging/lwo-status.json"
event="${GITHUB_EVENT_NAME:-local}"
ref="${GITHUB_REF:-}"
summary="${GITHUB_STEP_SUMMARY:-}"
tmp="$(mktemp)"

python3 - "$outcome" "$status" "$summary" "$event" "$tmp" << 'PY'
import json, sys
outcome, status_path, summary_path, event, tmp = sys.argv[1:6]
doc = None
try:
    with open(status_path, encoding="utf-8") as fh:
        doc = json.load(fh)
except (OSError, ValueError):
    doc = None
errors = []
if doc is None:
    errors.append("no status file was written")
else:
    errors.extend(str(e) for e in (doc.get("validationErrors") or []))
ok = outcome == "success" and doc is not None and doc.get("ok") is True and not errors
if outcome != "success" and not errors:
    errors.append("job status %s" % outcome)
lines = ["### LWO forecast", "", "- ok: %s" % ("true" if ok else "false"), "- event: %s" % event]
if doc:
    lines.append("- t0: %s" % doc.get("t0"))
    lines.append("- bytes: %s" % doc.get("bytesDownloaded"))
    models = ", ".join("%s %s" % (m.get("id"), m.get("run")) for m in (doc.get("models") or []))
    if models:
        lines.append("- models: %s" % models)
    ens = doc.get("ens") or {}
    if ens:
        lines.append("- ens: %s %s members %s" % (ens.get("src"), ens.get("run"), ens.get("members")))
if not ok:
    lines += ["", "ok: false", ""]
    lines.extend("- %s" % e for e in errors)
text = "\n".join(lines) + "\n"
sys.stdout.write(text)
if summary_path:
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(text)
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("1\n" if ok else "0\n")
    fh.write("\n".join(errors))
PY

publish_path=0
if [ "$event" = "schedule" ]; then publish_path=1; fi
if [ "$event" = "workflow_dispatch" ] && [ "$ref" = "refs/heads/main" ]; then publish_path=1; fi
if [ "$publish_path" != 1 ] || [ "$action" = "none" ]; then
    rm -f "$tmp"
    exit 0
fi
if [ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
    echo "[lwo-report] no token; skipped the issue"
    rm -f "$tmp"
    exit 0
fi
export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"

ok="$(head -n 1 "$tmp")"
err="$(tail -n +2 "$tmp")"
rm -f "$tmp"
title="LWO forecast refresh failed"
repo="${GITHUB_REPOSITORY:-}"
run_url="${GITHUB_SERVER_URL:-https://github.com}/${repo}/actions/runs/${GITHUB_RUN_ID:-0}"
num="$(gh issue list --repo "$repo" --state open --limit 20 --json number,title \
    --jq ".[] | select(.title==\"${title}\") | .number" 2>/dev/null | head -n 1 || true)"

if [ "$action" = "close" ] && [ "$ok" = 1 ]; then
    if [ -n "$num" ]; then
        gh issue close "$num" --repo "$repo" --reason completed \
            --comment "The refresh succeeded and the publish gate passed: ${run_url}" || true
    fi
    exit 0
fi
if [ "$action" = "open" ] && [ "$ok" != 1 ]; then
    body="$(printf 'ok: false\n\n%s\n\nRun: %s\n' "$err" "$run_url")"
    if [ -n "$num" ]; then
        gh issue comment "$num" --repo "$repo" --body "$body" || true
    else
        gh issue create --repo "$repo" --title "$title" --body "$body" || true
    fi
fi
exit 0
