#!/usr/bin/env python3
"""Gate for publishing fb-lwo-forecast v1.

The engine already refuses to write on a hard failure (exit 3, 4, 5, 6). This
check is the extra gate the scheduled job uses before replacing the Pages file:
every configured voting model is present and current, hours 0-47 are present,
values are in a physical range, and ECMWF ENS is present on every site.

A previous good file is left in place when this exits non-zero.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from typing import Optional

SCHEMA = "fb-lwo-forecast"
REQUIRED_MODELS = ("hrrr", "rrfs", "gfs", "ecmwf")
REQUIRED_SITES = ("cape", "starbase", "vandenberg", "starbase-la")
RULES = ("cu", "thick", "att", "det", "dist")
EXC_ROWS = ("g25", "g30", "g35", "g40", "g45", "r010", "r025", "r050", "r100")
HOURS = 48
BLOCKS = 8
MASK_RE = re.compile(r"^[0-9a-f]{96}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ENGINE_SOURCE = {
    "repo": "pmanougian-boop/flight-board",
    "commit": "97c33dcb4c4c4729a8b725352c173fab85c542e4",
    "path": "tools/lwo-engine/lwo_w3.py",
    "blob": "c394694340b970bd41c57abe7cfe0b50c4a87deb",
    "version": "0.1.0",
}


def parse_iso(s: str) -> dt.datetime:
    if not isinstance(s, str) or not ISO_RE.match(s):
        raise ValueError(f"not a UTC timestamp: {s!r}")
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _in_range(x, lo: float, hi: float) -> bool:
    return x is None or (_num(x) and lo <= float(x) <= hi)


def validate(doc: dict, *, now: Optional[dt.datetime] = None, require_fresh: bool = False,
             max_age_h: float = 6.0) -> list[str]:
    """Return a list of problems. Empty means the file may be published."""
    err: list[str] = []
    if not isinstance(doc, dict):
        return ["forecast is not a JSON object"]
    if doc.get("schema") != SCHEMA or doc.get("v") != 1:
        err.append(f"schema/v is {doc.get('schema')!r}/{doc.get('v')!r}")
    if doc.get("stepH") != 1 or doc.get("hours") != HOURS:
        err.append(f"stepH/hours is {doc.get('stepH')!r}/{doc.get('hours')!r}, want 1/{HOURS}")
    order = doc.get("modelOrder")
    if order != list(REQUIRED_MODELS):
        err.append(f"modelOrder is {order!r}, want {list(REQUIRED_MODELS)}")
    if doc.get("rules") != list(RULES):
        err.append(f"rules is {doc.get('rules')!r}")

    models = doc.get("models")
    by_id = {}
    if not isinstance(models, list):
        err.append("models is missing")
    else:
        for m in models:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                by_id[m["id"]] = m
        for mid in REQUIRED_MODELS:
            m = by_id.get(mid)
            if m is None:
                err.append(f"model {mid} is absent")
                continue
            if m.get("status") != "ok":
                err.append(f"model {mid} status is {m.get('status')!r}, want ok")
            try:
                run = parse_iso(m.get("run"))
            except ValueError as e:
                err.append(f"model {mid} run: {e}")
                run = None
            max_h = m.get("maxH")
            if not isinstance(max_h, int) or isinstance(max_h, bool) or not 0 <= max_h <= 72:
                err.append(f"model {mid} maxH {max_h!r} is outside 0-72")
            if run is not None and require_fresh and now is not None and run > now + dt.timedelta(hours=1):
                err.append(f"model {mid} run {m.get('run')} is in the future")

    t0 = None
    generated = None
    try:
        t0 = parse_iso(doc.get("t0"))
    except ValueError as e:
        err.append(f"t0: {e}")
    try:
        generated = parse_iso(doc.get("generatedAt"))
    except ValueError as e:
        err.append(f"generatedAt: {e}")
    if require_fresh and now is not None and generated is not None:
        age = abs((now - generated).total_seconds())
        if age > max_age_h * 3600:
            err.append(f"generatedAt is {age / 3600:.1f} h from now, over {max_age_h:g} h")
    if require_fresh and now is not None and t0 is not None:
        if t0 > now + dt.timedelta(minutes=5) or now - t0 > dt.timedelta(hours=max_age_h):
            err.append(f"t0 {doc.get('t0')} is not the current cycle window")
        if t0.minute or t0.second:
            err.append("t0 is not on an hour boundary")

    sites = doc.get("sites")
    if not isinstance(sites, list):
        err.append("sites is missing")
        sites = []
    seen = [s.get("id") for s in sites if isinstance(s, dict)]
    if set(seen) != set(REQUIRED_SITES) or len(seen) != len(REQUIRED_SITES):
        err.append(f"sites are {seen!r}, want {list(REQUIRED_SITES)}")
    ens_runs = set()
    n_models = len(REQUIRED_MODELS)
    bit_ceiling = (1 << n_models) - 1
    for s in sites:
        if not isinstance(s, dict):
            err.append("a site is not an object")
            continue
        sid = s.get("id")
        for key in ("avail", "ltg"):
            if not isinstance(s.get(key), str) or not MASK_RE.match(s.get(key, "")):
                err.append(f"{sid}.{key} is not 48 hours of hex (96 chars)")
        rules = s.get("rules") if isinstance(s.get("rules"), dict) else {}
        for rule in RULES:
            if not isinstance(rules.get(rule), str) or not MASK_RE.match(rules.get(rule, "")):
                err.append(f"{sid}.rules.{rule} is not 48 hours of hex")
        avail = s.get("avail") if isinstance(s.get("avail"), str) else ""
        if MASK_RE.match(avail):
            weak = []
            for i in range(HOURS):
                cell = int(avail[2 * i:2 * i + 2], 16)
                if cell & ~bit_ceiling:
                    err.append(f"{sid} hour {i} avail uses a bit outside the {n_models} models")
                    break
                if i <= 40 and bin(cell).count("1") < 3:
                    weak.append(i)
            if len(weak) > 6:
                err.append(f"{sid} has {len(weak)} hours in 0-40 with fewer than 3 models")
        gust = s.get("gust") if isinstance(s.get("gust"), dict) else {}
        for lane in ("med", "lo", "hi"):
            arr = gust.get(lane)
            if not isinstance(arr, list) or len(arr) != HOURS:
                err.append(f"{sid}.gust.{lane} is not {HOURS} values")
            elif any(not _in_range(v, 0, 200) for v in arr):
                err.append(f"{sid}.gust.{lane} has a value outside 0-200 kt")
        pw = s.get("pw")
        if not isinstance(pw, list) or len(pw) != HOURS:
            err.append(f"{sid}.pw is not {HOURS} values")
        elif any(not _in_range(v, 0, 800) for v in pw):
            err.append(f"{sid}.pw has a value outside 0-8.00 in")
        shear = s.get("shear") if isinstance(s.get("shear"), dict) else {}
        kt = shear.get("kt")
        if shear.get("stepH") != 6 or not isinstance(kt, list) or len(kt) != BLOCKS:
            err.append(f"{sid}.shear is not 8 values at 6 h")
        elif any(not _in_range(v, 0, 250) for v in kt):
            err.append(f"{sid}.shear has a value outside 0-250 kt")
        exc = s.get("exc")
        if not isinstance(exc, dict):
            err.append(f"{sid} has no ECMWF ENS block")
            continue
        if exc.get("src") != "ecmwf-ens":
            err.append(f"{sid}.exc.src is {exc.get('src')!r}")
        members = exc.get("members")
        if not isinstance(members, int) or isinstance(members, bool) or not 40 <= members <= 50:
            err.append(f"{sid}.exc.members is {members!r}, want 40-50")
            members = 50
        if exc.get("blockH") != 6 or exc.get("rowsDef") != list(EXC_ROWS):
            err.append(f"{sid}.exc shape is not 6 h blocks of {list(EXC_ROWS)}")
        try:
            ens_runs.add(parse_iso(exc.get("run")).strftime("%Y-%m-%dT%H:%MZ"))
        except ValueError as e:
            err.append(f"{sid}.exc.run: {e}")
        k = exc.get("k")
        if not isinstance(k, list) or len(k) != len(EXC_ROWS):
            err.append(f"{sid}.exc.k is not {len(EXC_ROWS)} rows")
        else:
            filled = 0
            for row in k:
                if not isinstance(row, list) or len(row) != BLOCKS:
                    err.append(f"{sid}.exc.k row is not {BLOCKS} blocks")
                    break
                for v in row:
                    if v is None:
                        continue
                    filled += 1
                    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= members:
                        err.append(f"{sid}.exc.k value {v!r} is outside 0..{members}")
                        break
            if filled < len(EXC_ROWS) * 6:
                err.append(f"{sid}.exc has only {filled} filled cells; ENS does not cover the window")
    if len(ens_runs) > 1:
        err.append(f"ECMWF ENS run differs across sites: {sorted(ens_runs)}")
    if not ens_runs:
        err.append("ECMWF ENS is not present")
    return err


def _model_brief(doc: Optional[dict]) -> list[dict]:
    if not doc:
        return []
    out = []
    for m in doc.get("models") or []:
        if isinstance(m, dict):
            out.append({"id": m.get("id"), "run": m.get("run"), "status": m.get("status"), "maxH": m.get("maxH")})
    return out


def _ens_brief(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    for s in doc.get("sites") or []:
        exc = s.get("exc") if isinstance(s, dict) else None
        if isinstance(exc, dict):
            k = exc.get("k") or []
            present = 0
            if k and isinstance(k[0], list):
                present = sum(1 for v in k[0] if v is not None)
            return {"src": exc.get("src"), "run": exc.get("run"), "members": exc.get("members"),
                    "blocksPresent": present}
    return None


def _changed_hours(live: Optional[dict], staging: Optional[dict]) -> dict:
    if not live or not staging:
        return {}
    live_sites = {s.get("id"): s for s in live.get("sites") or [] if isinstance(s, dict)}
    out = {}
    for s in staging.get("sites") or []:
        if not isinstance(s, dict):
            continue
        prev = live_sites.get(s.get("id")) or {}
        pairs = [("avail", s.get("avail"), prev.get("avail")),
                 ("ltg", s.get("ltg"), prev.get("ltg"))]
        for rule in RULES:
            pairs.append((rule, (s.get("rules") or {}).get(rule), (prev.get("rules") or {}).get(rule)))
        for key, a, b in pairs:
            if not isinstance(a, str) or not isinstance(b, str) or len(a) != len(b) or len(a) % 2:
                continue
            n = sum(1 for i in range(0, len(a), 2) if a[i:i + 2] != b[i:i + 2])
            out[f"{s.get('id')}.{key}"] = n
    return out


def parse_log(text: str) -> dict:
    info = {"bytesDownloaded": None, "requests": None, "bytesWritten": None, "plannedBytes": None,
            "durationSecLogged": None}
    m = re.search(r"downloaded (\d+) bytes in (\d+) requests", text)
    if m:
        info["bytesDownloaded"] = int(m.group(1))
        info["requests"] = int(m.group(2))
    m = re.search(r"wrote (\d+) bytes", text)
    if m:
        info["bytesWritten"] = int(m.group(1))
    m = re.search(r"plan: (\d+) bytes", text)
    if m:
        info["plannedBytes"] = int(m.group(1))
    m = re.search(r"downloaded \d+ bytes in \d+ requests; (\d+) s", text)
    if m:
        info["durationSecLogged"] = int(m.group(1))
    return info


def build_status(*, forecast: Optional[dict], errors: list[str], engine_exit: int, log_info: dict,
                 started: dt.datetime, finished: dt.datetime, event: str, published: bool) -> dict:
    return {
        "schema": "fb-lwo-status",
        "v": 1,
        "ok": engine_exit == 0 and not errors and forecast is not None,
        "published": published,
        "event": event,
        "generatedAt": None if not forecast else forecast.get("generatedAt"),
        "t0": None if not forecast else forecast.get("t0"),
        "startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finishedAt": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "durationSec": int((finished - started).total_seconds()),
        "engineExit": engine_exit,
        "bytesDownloaded": log_info.get("bytesDownloaded"),
        "requests": log_info.get("requests"),
        "plannedBytes": log_info.get("plannedBytes"),
        "bytesWritten": log_info.get("bytesWritten"),
        "models": _model_brief(forecast),
        "ens": _ens_brief(forecast),
        "hours": {"count": HOURS, "from": 0, "to": HOURS - 1},
        "sites": REQUIRED_SITES,
        "validationErrors": errors,
        "engine": ENGINE_SOURCE,
    }


def build_diff(live: Optional[dict], staging: Optional[dict], live_bytes: int, staging_bytes: int) -> dict:
    def brief(doc, n):
        if not doc:
            return None
        return {"generatedAt": doc.get("generatedAt"), "t0": doc.get("t0"), "bytes": n,
                "cfgHash": doc.get("cfgHash"), "models": _model_brief(doc), "ens": _ens_brief(doc)}
    shift = None
    if live and staging:
        try:
            shift = int((parse_iso(staging["t0"]) - parse_iso(live["t0"])).total_seconds() // 3600)
        except (KeyError, ValueError):
            shift = None
    return {"live": brief(live, live_bytes), "staging": brief(staging, staging_bytes),
            "t0ShiftHours": shift, "hoursChanged": _changed_hours(live, staging)}


def _load(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate an LWO forecast and write the run status.")
    ap.add_argument("--forecast", required=True)
    ap.add_argument("--prev", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--status", required=True)
    ap.add_argument("--diff", default=None)
    ap.add_argument("--engine-exit", type=int, required=True)
    ap.add_argument("--started-epoch", type=int, required=True)
    ap.add_argument("--finished-epoch", type=int, required=True)
    ap.add_argument("--event", default="local")
    ap.add_argument("--published", action="store_true")
    ap.add_argument("--require-fresh", action="store_true")
    a = ap.parse_args(argv)
    started = dt.datetime.fromtimestamp(a.started_epoch, dt.timezone.utc).replace(microsecond=0)
    finished = dt.datetime.fromtimestamp(a.finished_epoch, dt.timezone.utc).replace(microsecond=0)
    log_text = ""
    if a.log:
        try:
            with open(a.log, "r", encoding="utf-8", errors="replace") as fh:
                log_text = fh.read()
        except OSError:
            log_text = ""
    log_info = parse_log(log_text)
    forecast = _load(a.forecast)
    errors: list[str] = []
    if a.engine_exit != 0:
        errors.append(f"engine exit {a.engine_exit}")
    if forecast is None:
        errors.append("forecast file is missing or is not JSON")
    else:
        errors.extend(validate(forecast, now=finished, require_fresh=a.require_fresh))
    status = build_status(forecast=forecast, errors=errors, engine_exit=a.engine_exit, log_info=log_info,
                          started=started, finished=finished, event=a.event, published=bool(a.published) and not errors)
    with open(a.status, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2)
        fh.write("\n")
    if a.diff:
        live = _load(a.prev)
        live_n = 0
        staging_n = 0
        try:
            if a.prev:
                with open(a.prev, "rb") as fh:
                    live_n = len(fh.read())
        except OSError:
            live_n = 0
        try:
            with open(a.forecast, "rb") as fh:
                staging_n = len(fh.read())
        except OSError:
            staging_n = 0
        with open(a.diff, "w", encoding="utf-8") as fh:
            json.dump(build_diff(live, forecast, live_n, staging_n), fh, indent=2)
            fh.write("\n")
    if errors:
        print("[lwo-validate] rejected:", "; ".join(errors), file=sys.stderr)
        return 1
    print(f"[lwo-validate] ok t0={status['t0']} bytes={log_info.get('bytesDownloaded')} "
          f"models={','.join(m['id'] for m in status['models'])} ens={status['ens']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
