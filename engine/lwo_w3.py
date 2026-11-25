#!/usr/bin/env python3
"""W3 LWO forecast engine (Flight Board 12.0, C5 part 2 of 5).

Offline, standalone. Runs on Phil's Mac mini as a packaged copy (lwo_w3.pyz),
never on Render and never imported by the server. It:

  * fetches ONLY public NOAA (AWS Open Data) and ECMWF open-data GRIB2, with
    HTTPS GET byte-range requests driven by each file's .idx / .index;
  * decodes the selected messages (eccodes, behind the Decoder interface);
  * samples great-circle nmi circles (5 / 10 nmi, plus a 60 nmi storm search
    that is OUR heuristic) around each configured pad;
  * votes 5 rule proxies of NASA-STD-4010B per model-hour (k of n models,
    never a percent, never a commit call);
  * encodes per-hour 8-bit model masks (two hex chars per hour);
  * enforces the 60 KB cap (degrade order of design 3.3, else write nothing,
    exit 3) and an allow-list of keys;
  * writes ONE local JSON file. It never uploads anything.
  * refuses to start (exit 6) with under 10 GB free on the home or output
    volume; GRIB and index bytes live in memory only (no temp files).

It needs no secrets and no repo checkout. Logs carry run ids, byte counts and
timings only (no hostnames, usernames, IPs or paths).

Advisory model guidance only. Not the official Launch Weather Officer
evaluation. Every rule here is a PROXY of a NASA-STD-4010B (Approved
2026-08-27) criterion; 14 CFR 450.163(a)(1) is the current FAA hook and sets
no numbers itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "0.1.0"

SCHEMA = "fb-lwo-forecast"
SCHEMA_V = 1
MAX_BYTES = 60 * 1024
TARGET_BYTES = 45 * 1024
HOURS = 48                   # Phil 2026-11-17 7:18 PM CT: the board shows 0-48 h only
MAX_SITES = 8
MAX_MODELS = 6
RULE_IDS = ("cu", "thick", "att", "det", "dist")
EXC_ROWS = ("g25", "g30", "g35", "g40", "g45", "r010", "r025", "r050", "r100")
EXIT_OK, EXIT_USAGE, EXIT_OVER_BUDGET, EXIT_NO_MODELS, EXIT_DOWNLOAD_CAP = 0, 2, 3, 4, 5
EXIT_LOW_DISK = 6            # Phil 2026-11-17 8:15 PM CT: free space under minFreeGB, before any network call
DEFAULT_MIN_FREE_GB = 10.0   # 1e10 bytes free on the home and the output volume, at least
# Horizon (Phil 2026-11-17 8:14 PM CT, "A"): every model is capped by VALID TIME, t0 + 48 h, not by
# forecast hour: a cycle A h old is fetched to f(48 + A), never past its native horizon. The
# latest cycle is at most 24 h old (the probe lookback), so no step is ever past f072.
MAX_LEAD_H = HOURS + 24      # absolute forecast-hour ceiling (48 h window + 24 h lookback)
LEAD_OUT_MAX_H = 3           # the lead-out bracket step is at most 3 h past the last output hour
DEFAULT_MAX_DOWNLOAD_GB = 4.0  # Lead 2026-11-17: hard per-run download cap (planned and actual)
GB = 10 ** 9                   # decimal: maxDownloadGB 4.0 = 4.0e9 bytes (Lead 2026-11-17), not GiB
# Step schedule (Lead/Phil 2026-11-17): basis "board" (default): hourly for the first 7 h from t0
# (hour index 0-7), then 3-hourly to t0 + 48 h (valid time). A native step fh is kept
# when, in the first segment with x <= toH, fh % stepH == 0 (x = hour index from t0 for basis
# "board"; x = forecast hour for basis "fh"). ENS has its own steps (6-hourly to t0 + 48 h).
# Hours between
# two fetched steps are filled conservatively (fill_between_steps). Native model cadence still
# applies (a model never has a step it does not publish), and every step is capped at the
# model's horizon and fh_cap() (valid time t0 + 48 h, at most f072).
DEFAULT_SCHEDULE = {"basis": "board", "segments": ({"toH": 7, "stepH": 1}, {"toH": 48, "stepH": 3})}

EARTH_R_NMI = 3440.065
MS_TO_KT = 1.943844
MM_PER_IN = 25.4

ADVISORY = ("Model-only advisory. k of n models agree; not a probability; not verified. "
            "Proxies of NASA-STD-4010B; not the official LWO evaluation.")

# NASA-STD-4010B (Approved 2026-08-27) sections each proxy stands in for.
RULE_CITES = {
    "cu": "4010B 4.1.3 (LLCCR 9-11) cumulus: 4.1.3.1.b pad -5 C, 4.1.3.2 <=5 nmi -10 C, 4.1.3.3 >5-10 nmi -20 C (proxy)",
    "thick": "4010B 4.1.8 (LLCCR 22-24) thick cloud: 4.1.8.1.a/b in the 5 nmi ring, 4.1.8 lead-in combining "
             "(precip connect 15 dBZ / 0.01 in/h ours (proxy), composite REFC), 4.1.8.2 by proxy (proxy)",
    "att": "4010B 4.1.4 (LLCCR 12-14) attached anvil likely within 10 nmi; no exemptions or waits (proxy)",
    "det": "4010B 4.1.5 (LLCCR 15-17) detached anvil, 60 nmi / 2 h upwind search ours; no exemptions or waits (proxy)",
    "dist": "4010B 4.1.7 (LLCCR 21) disturbed weather, moderate precip per 4.2.2.b(2)-(3) (proxy)",
    "ltg": "4010B 4.1.1.1-4.1.1.2 (LLCCR 5-6) lightning, forecast lane only (proxy)",
}

# The only hosts the engine will ever contact (HTTPS GET only).
ALLOWED_HOSTS = frozenset({
    "noaa-hrrr-bdp-pds.s3.amazonaws.com",
    "noaa-rrfs-ops-pds.s3.amazonaws.com",
    "noaa-gfs-bdp-pds.s3.amazonaws.com",
    "data.ecmwf.int",
    "noaa-nbm-grib2-pds.s3.amazonaws.com",   # optional, off by default
    "noaa-gefs-pds.s3.amazonaws.com",        # optional, off by default
})

# Default thresholds. Numbers from 4010B unless marked "ours". All configurable.
DEFAULT_RULES = {
    "cloudRh": 90.0,            # ours: RH (%) that defines a model cloud level
    "cuCapeMin": 100.0,         # ours: CAPE J/kg that makes a cloud "cumulus"
    "cuBaseTolM": 500.0,        # ours: cloud base within this of the LCL
    "cuPadTopC": -5.0,          # 4.1.3.1.b
    "cu5TopC": -10.0,           # 4.1.3.2
    "cu10TopC": -20.0,          # 4.1.3.3
    "thickMinM": 1400.0,        # 4.1.8.1 (1.4 km / 4,500 ft)
    "thickWarmC": 0.0,          # 4.1.8.1, inclusive
    "thickColdC": -20.0,        # 4.1.8.1, inclusive
    "cirriformMaxC": -15.0,     # 4.1.8.2
    "clwZero": 1e-6,            # ours: kg/kg at or below which "no liquid water"
    "anvilTopC": -10.0,         # 4.1.4 / 4.1.5 lead-in: parent top <= -10 C
    "anvilPMax": 400.0,         # ours: anvil-level cloud = levels at or above 400 hPa
    "anvilPMin": 150.0,         # ours
    "coreRefcDbz": 35.0,        # ours (fine models)
    "coreCapeCoarse": 1000.0,   # ours (coarse models, with a convective precip signal)
    "detSearchNmi": 60.0,       # ours: storm search radius
    "detTravelH": 2.0,          # ours: upwind travel time on the 250 hPa wind
    "detUpwindDeg": 45.0,       # ours: half-angle of the upwind cone
    "distTopC": 0.0,            # 4.1.7: tops colder than 0 C (strict)
    "distRefcDbz": 30.0,        # 4.2.2.b: 30 dBZ or greater
    "distPrecipIn": 0.10,       # 4.2.2.b: greater than 0.1 in/h (1-h accumulation is ours)
    # ours (proxy). NASA-STD-4010B 4.1.8 lead-in (and 3.2 "Thick Cloud Layer"): layers "must be
    # combined if they are physically connected by vertically continuous clouds or by
    # precipitation" -- 4010B gives no dBZ / rate for "by precipitation", so these numbers are ours. Site Weather
    # proxy review 2026-11-17 kept these (light precip ~15-20 dBZ / any measurable; NOT the 30 dBZ /
    # 0.1 in/h "moderate" numbers of 4.2.2.b). Composite REFC only: virga or an elevated echo can
    # over-connect. Only ever ADDS a combined layer; never required for the base thick vote.
    "precipConnectDbz": 15.0,   # ours (proxy): column composite REFC (dBZ) that connects layers
    "precipConnectIn": 0.01,    # ours (proxy): column 1-h surface precip (in) that connects layers
}

# --------------------------------------------------------------------------
# Model registry. Adding/removing a KNOWN id is config-only; unknown ids are refused.
# --------------------------------------------------------------------------

LEVELS_DEFAULT = (1000, 925, 850, 700, 600, 500, 400, 300, 250, 200)
CLW_LEVELS = (500, 400, 300, 250, 200)
WIND_LEVELS = (925, 500, 250)


@dataclass(frozen=True)
class FileSpec:
    grib: str           # url template; {ymd} {hh} {fff} {fh} {ymdhh}
    index: str          # url template of the .idx / .index
    kind: str           # "noaa" | "ecmwf"
    fields: Tuple[str, ...]  # canonical fields fetched from this file


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    coarse: bool
    votes: bool          # contributes rule votes and avail bits
    ltg: bool            # has a forecast lightning field
    cycles: Tuple[int, ...]
    horizon: Dict[int, int]   # cycle hour -> max fh (default key -1)
    max_age_h: float
    files: Tuple[FileSpec, ...]
    steps: Callable[[int], List[int]] = field(compare=False, default=None)  # horizon -> fh list
    attribution: str = ""
    levels: Tuple[int, ...] = LEVELS_DEFAULT

    def horizon_for(self, cycle: int) -> int:
        return self.horizon.get(cycle, self.horizon.get(-1, 0))


def _hourly(h):
    return list(range(0, h + 1))


def _gfs_steps(h):
    # GFS 0.5 deg (pgrb2.0p50) is 3-hourly from f000 (f001/f002/f004 indexes 404, f003 206;
    # checked 2026-11-17 12Z).
    return list(range(0, h + 1, 3))


def _ecmwf_steps(h):
    return [f for f in range(0, min(h, 144) + 1, 3)] + [f for f in range(150, h + 1, 6)]


def _gefs_steps(h):
    return [f for f in range(0, min(h, 240) + 1, 3)] + [f for f in range(246, h + 1, 6)]


def _nbm_steps(h):  # unverified cadence; a missing .idx just clears that hour
    return [f for f in range(1, min(h, 36) + 1)] + [f for f in range(39, h + 1, 3)]


S3 = "https://{host}.s3.amazonaws.com/"
NOAA_ATTR = "NOAA NCEP {} via NOAA Open Data Dissemination (AWS)"

MODELS: Dict[str, ModelSpec] = {
    "hrrr": ModelSpec(
        id="hrrr", label="HRRR", coarse=False, votes=True, ltg=True,
        # B (Phil 2026-11-17 8:14 PM CT): the newest 00/06/12/18Z cycle, the ones that reach f048,
        # instead of a newer off-hour f018 cycle. Max age 11 h = 6 h cadence + up to 5 h lag.
        cycles=(0, 6, 12, 18), horizon={0: 48, 6: 48, 12: 48, 18: 48, -1: 18}, max_age_h=11,
        files=(
            FileSpec(S3.format(host="noaa-hrrr-bdp-pds") + "hrrr.{ymd}/conus/hrrr.t{hh}z.wrfprsf{ff}.grib2",
                     S3.format(host="noaa-hrrr-bdp-pds") + "hrrr.{ymd}/conus/hrrr.t{hh}z.wrfprsf{ff}.grib2.idx",
                     "noaa", ("t", "rh", "z", "clw", "u", "v")),
            FileSpec(S3.format(host="noaa-hrrr-bdp-pds") + "hrrr.{ymd}/conus/hrrr.t{hh}z.wrfsfcf{ff}.grib2",
                     S3.format(host="noaa-hrrr-bdp-pds") + "hrrr.{ymd}/conus/hrrr.t{hh}z.wrfsfcf{ff}.grib2.idx",
                     "noaa", ("cape", "refc", "retop", "gust", "ltng", "apcp", "pwat")),
        ),
        steps=_hourly, attribution="HRRR"),
    "rrfs": ModelSpec(
        id="rrfs", label="RRFS", coarse=False, votes=True, ltg=True,
        # B: synoptic cycles only (native f084, verified at 12Z); the valid-time cap (A) keeps it to
        # t0 + 48 h, i.e. f(48 + age). Max age 11 h as HRRR.
        cycles=(0, 6, 12, 18), horizon={0: 84, 6: 84, 12: 84, 18: 84, -1: 18}, max_age_h=11,
        files=(
            FileSpec(S3.format(host="noaa-rrfs-ops-pds") + "rrfs.{ymd}/{hh}/rrfs.t{hh}z.prslev.3km.f{fff}.conus.grib2",
                     S3.format(host="noaa-rrfs-ops-pds") + "rrfs.{ymd}/{hh}/rrfs.t{hh}z.prslev.3km.f{fff}.conus.grib2.idx",
                     "noaa", ("t", "rh", "z", "clw", "u", "v")),
            FileSpec(S3.format(host="noaa-rrfs-ops-pds") + "rrfs.{ymd}/{hh}/rrfs.t{hh}z.2dfld.3km.f{fff}.conus.grib2",
                     S3.format(host="noaa-rrfs-ops-pds") + "rrfs.{ymd}/{hh}/rrfs.t{hh}z.2dfld.3km.f{fff}.conus.grib2.idx",
                     "noaa", ("cape", "refc", "retop", "gust", "ltng", "apcp", "pwat")),
        ),
        steps=_hourly, attribution="RRFS"),
    "gfs": ModelSpec(
        id="gfs", label="GFS", coarse=True, votes=True, ltg=False,
        cycles=(0, 6, 12, 18), horizon={-1: 384}, max_age_h=14,
        files=(
            FileSpec(S3.format(host="noaa-gfs-bdp-pds") + "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p50.f{fff}",
                     S3.format(host="noaa-gfs-bdp-pds") + "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p50.f{fff}.idx",
                     "noaa", ("t", "rh", "z", "clw", "u", "v", "cape", "gust", "apcp", "cprate", "pwat")),
        ),
        steps=_gfs_steps, attribution="GFS"),
    "ecmwf": ModelSpec(
        id="ecmwf", label="ECMWF", coarse=True, votes=True, ltg=False,
        cycles=(0, 6, 12, 18), horizon={0: 360, 12: 360, -1: 90}, max_age_h=20,
        files=(
            FileSpec("https://data.ecmwf.int/forecasts/{ymd}/{hh}z/ifs/0p25/oper/{ymdhh}0000-{fh}h-oper-fc.grib2",
                     "https://data.ecmwf.int/forecasts/{ymd}/{hh}z/ifs/0p25/oper/{ymdhh}0000-{fh}h-oper-fc.index",
                     "ecmwf", ("t", "rh", "z", "u", "v", "cape", "gust", "apcp", "cpacc", "pwat")),
        ),
        steps=_ecmwf_steps, attribution=""),
    # Optional, OFF by default (Phil Q4). URLs verified by a single .idx GET on 2026-11-17.
    "gefs": ModelSpec(
        id="gefs", label="GEFS", coarse=True, votes=True, ltg=False,
        cycles=(0, 6, 12, 18), horizon={-1: 384}, max_age_h=14,
        files=(
            FileSpec(S3.format(host="noaa-gefs-pds") + "gefs.{ymd}/{hh}/atmos/pgrb2ap5/gec00.t{hh}z.pgrb2a.0p50.f{fff}",
                     S3.format(host="noaa-gefs-pds") + "gefs.{ymd}/{hh}/atmos/pgrb2ap5/gec00.t{hh}z.pgrb2a.0p50.f{fff}.idx",
                     "noaa", ("t", "rh", "z", "u", "v", "cape", "apcp", "pwat")),
        ),
        steps=_gefs_steps, attribution="GEFS",
        levels=(1000, 925, 850, 700, 500, 250, 200)),
    # NBM has no column profiles, so it never votes and never sets avail bits;
    # it only contributes to the gust spread. Optional, OFF by default.
    "nbm": ModelSpec(
        id="nbm", label="NBM", coarse=True, votes=False, ltg=False,
        cycles=tuple(range(24)), horizon={-1: 192}, max_age_h=8,
        files=(
            FileSpec(S3.format(host="noaa-nbm-grib2-pds") + "blend.{ymd}/{hh}/core/blend.t{hh}z.core.f{fff}.co.grib2",
                     S3.format(host="noaa-nbm-grib2-pds") + "blend.{ymd}/{hh}/core/blend.t{hh}z.core.f{fff}.co.grib2.idx",
                     "noaa", ("gust",)),
        ),
        steps=_nbm_steps, attribution="NBM"),
}
MODEL_BIT_ORDER = ("hrrr", "rrfs", "gfs", "ecmwf", "nbm", "gefs")  # bit 0..5 when present
DEFAULT_MODELS = ("hrrr", "rrfs", "gfs", "ecmwf")

ENS_FILE = FileSpec(
    "https://data.ecmwf.int/forecasts/{ymd}/{hh}z/ifs/0p25/enfo/{ymdhh}0000-{fh}h-enfo-ef.grib2",
    "https://data.ecmwf.int/forecasts/{ymd}/{hh}z/ifs/0p25/enfo/{ymdhh}0000-{fh}h-enfo-ef.index",
    "ecmwf", ("gust", "apcp"))

# --------------------------------------------------------------------------
# Index parsing (.idx = NOAA wgrib2 inventory, .index = ECMWF JSON lines)
# --------------------------------------------------------------------------


@dataclass
class IdxEntry:
    offset: int
    end: Optional[int]  # inclusive; None = to end of file
    var: str
    level: str
    fcst: str
    extra: str = ""
    number: Optional[str] = None  # ECMWF ensemble member

    @property
    def length(self) -> Optional[int]:
        return None if self.end is None else self.end - self.offset + 1


def parse_noaa_idx(text: str) -> List[IdxEntry]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 6 or not parts[1].isdigit():
            continue
        extra = ":".join(p for p in parts[6:] if p)
        rows.append(IdxEntry(int(parts[1]), None, parts[3], parts[4], parts[5], extra))
    # Offsets are not always in line order (RRFS), so sort before computing ends.
    rows.sort(key=lambda e: e.offset)
    for a, b in zip(rows, rows[1:]):
        a.end = b.offset - 1
    return rows


def parse_ecmwf_index(text: str) -> List[IdxEntry]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        off, ln = int(d["_offset"]), int(d["_length"])
        lev = d.get("levtype", "") + ":" + str(d.get("levelist", ""))
        rows.append(IdxEntry(off, off + ln - 1, d.get("param", ""), lev, str(d.get("step", "")),
                             d.get("type", ""), d.get("number")))
    rows.sort(key=lambda e: e.offset)
    return rows


_INST = re.compile(r"^(\d+) hour fcst$")
_WIN = re.compile(r"^(\d+)-(\d+) (hour|day) (acc|max|ave) fcst$")


def fcst_window(fcst: str) -> Optional[Tuple[int, int, str]]:
    """'5-6 hour acc fcst' -> (5, 6, 'acc'); '6 hour fcst' -> (6, 6, 'inst'); 'anl' -> (0,0,'inst')."""
    if fcst == "anl":
        return (0, 0, "inst")
    m = _INST.match(fcst)
    if m:
        h = int(m.group(1))
        return (h, h, "inst")
    m = _WIN.match(fcst)
    if m:
        k = 24 if m.group(3) == "day" else 1
        return (int(m.group(1)) * k, int(m.group(2)) * k, m.group(4))
    return None


# canonical field -> (var, set of acceptable levels or "PL" for pressure levels, kind)
NOAA_FIELDS = {
    "t": ("TMP", "PL", "inst"), "rh": ("RH", "PL", "inst"), "z": ("HGT", "PL", "inst"),
    "clw": ("CLMR", "PL", "inst"), "u": ("UGRD", "PL", "inst"), "v": ("VGRD", "PL", "inst"),
    "cape": ("CAPE", {"surface", "180-0 mb above ground"}, "inst"),
    "refc": ("REFC", {"entire atmosphere", "entire atmosphere (considered as a single layer)"}, "inst"),
    "retop": ("RETOP", {"cloud top", "entire atmosphere (considered as a single layer)"}, "inst"),
    "gust": ("GUST", {"surface", "10 m above ground"}, "inst"),
    "ltng": ("LTNG", {"entire atmosphere", "entire atmosphere (considered as a single layer)"}, "instmax"),
    "apcp": ("APCP", {"surface"}, "acc"),
    "cprate": ("CPRAT", {"surface"}, "inst"),
    "pwat": ("PWAT", {"entire atmosphere (considered as a single layer)"}, "inst"),
}
ECMWF_FIELDS = {
    "t": ("t", "pl"), "rh": ("r", "pl"), "z": ("gh", "pl"), "u": ("u", "pl"), "v": ("v", "pl"),
    "cape": ("mucape", "sfc"), "gust": ("10fg", "sfc"), "apcp": ("tp", "sfc"),
    "cpacc": ("cp", "sfc"), "pwat": ("tcwv", "sfc"),
}


@dataclass(frozen=True)
class Want:
    field: str
    level: Optional[int]   # hPa for pressure-level fields
    entry: IdxEntry
    window: Tuple[int, int] = (0, 0)  # accumulation window (hours) for acc fields


_ENS_TAG = re.compile(r"^ENS=[^:]*$")


def select_noaa(entries: Sequence[IdxEntry], fields: Iterable[str], fh: int,
                levels: Sequence[int]) -> List[Want]:
    out: List[Want] = []
    for f in fields:
        var, lev, kind = NOAA_FIELDS[f]
        lv = CLW_LEVELS if f == "clw" else (WIND_LEVELS if f in ("u", "v") else levels)
        best: Dict[Optional[int], Tuple[int, IdxEntry, Tuple[int, int]]] = {}
        for e in entries:
            # extra = probability/percentile/std-dev products: never used. GEFS member files tag
            # every line "ENS=low-res ctl" / "ENS=+1"; that names the member, so it is allowed.
            if e.var != var or (e.extra and not _ENS_TAG.match(e.extra)):
                continue
            w = fcst_window(e.fcst)
            if w is None:
                continue
            a, b, k = w
            if lev == "PL":
                m = re.match(r"^(\d+(?:\.\d+)?) mb$", e.level)
                if not m or float(m.group(1)) not in lv:
                    continue
                p: Optional[int] = int(float(m.group(1)))
            else:
                if e.level not in lev:
                    continue
                p = None
            if kind == "inst" and not (k == "inst" and b == fh):
                continue
            if kind == "instmax" and not (b == fh and k in ("inst", "max")):
                continue
            if kind == "acc" and not (k == "acc" and b == fh and b > a):
                continue
            span = b - a
            if p not in best or span < best[p][0]:   # shortest accumulation window wins
                best[p] = (span, e, (a, b))
        for p, (_, e, win) in sorted(best.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            out.append(Want(f, p, e, win))
    return out


def select_ecmwf(entries: Sequence[IdxEntry], fields: Iterable[str], fh: int,
                 levels: Sequence[int], member: Optional[str] = None) -> List[Want]:
    out = []
    for f in fields:
        param, levtype = ECMWF_FIELDS[f]
        lv = WIND_LEVELS if f in ("u", "v") else levels
        for e in entries:
            if e.var != param or e.fcst != str(fh) or not e.level.startswith(levtype + ":"):
                continue
            if member is not None and e.number != member:
                continue
            p = None
            if levtype == "pl":
                ls = e.level.split(":", 1)[1]
                if not ls.isdigit() or int(ls) not in lv:
                    continue
                p = int(ls)
            win = (0, fh) if f in ("apcp", "cpacc") else (0, 0)
            out.append(Want(f, p, e, win))
    return out


def merge_ranges(wants: Sequence[Want], max_gap: int = 0) -> List[Tuple[int, Optional[int], List[Want]]]:
    """Group wanted messages into as few HTTP ranges as possible (adjacent only by default)."""
    ws = sorted(wants, key=lambda w: w.entry.offset)
    groups: List[Tuple[int, Optional[int], List[Want]]] = []
    for w in ws:
        if groups and groups[-1][1] is not None and w.entry.offset - groups[-1][1] - 1 <= max_gap:
            s, _, lst = groups[-1]
            lst.append(w)
            groups[-1] = (s, w.entry.end, lst)
        else:
            groups.append((w.entry.offset, w.entry.end, [w]))
    return groups

# --------------------------------------------------------------------------
# HTTPS GET fetcher (public hosts only, no auth, no upload)
# --------------------------------------------------------------------------


class FetchError(Exception):
    """status: the HTTP status, or -1 for a transport/TLS error. reason: why, in words. url: what."""
    def __init__(self, status: int, msg: str = "", url: str = ""):
        super().__init__(msg or f"HTTP {status}")
        self.status = status
        self.reason = msg or _HTTP_REASON.get(status, f"HTTP {status}")
        self.url = url


_HTTP_REASON = {404: "HTTP 404: not published (yet)", 403: "HTTP 403: forbidden", 416: "HTTP 416: range not satisfiable",
                429: "HTTP 429: rate limited", 500: "HTTP 500", 502: "HTTP 502", 503: "HTTP 503", 504: "HTTP 504"}
REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 3
PROBE_LOOKBACK_H = 24   # "latest cycle" = the newest PUBLISHED cycle within the last 24 h


def tls_context() -> Tuple[ssl.SSLContext, str]:
    """The system default trust store, plus certifi's Mozilla bundle when certifi is installed.
    A Python whose OpenSSL bundle lacks a newer root (data.ecmwf.int chains to HARICA TLS RSA
    Root CA 2021; the AWS buckets chain to Amazon Root CA 1) would otherwise fail only on ECMWF."""
    ctx = ssl.create_default_context()
    note = "system trust store"
    try:
        import certifi  # pinned in requirements.lock
        ctx.load_verify_locations(cafile=certifi.where())
        note = f"system trust store + certifi {getattr(certifi, '__version__', '?')}"
    except Exception:   # certifi missing or unreadable: keep the system store only
        pass
    # Verification is never turned off (Lead 2026-11-17): a TLS error is reported, not bypassed.
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RuntimeError("TLS context must verify certificates and host names")
    return ctx, note


class DownloadCapExceeded(Exception):
    """The per-run download cap (maxDownloadGB, default 4) was or would be exceeded."""


class Fetcher:
    """GET-only client. Refuses non-HTTPS URLs and hosts outside ALLOWED_HOSTS."""

    def __init__(self, max_bytes: int = int(DEFAULT_MAX_DOWNLOAD_GB * GB), timeout: float = 60.0, retries: int = 2,
                 allowed_hosts: frozenset = ALLOWED_HOSTS):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.retries = retries
        self.allowed = allowed_hosts
        self.bytes = 0
        self.requests = 0
        self._conns: Dict[str, http.client.HTTPSConnection] = {}
        self._ctx: Optional[ssl.SSLContext] = None
        self.tls_note = ""

    def _check(self, url: str) -> Tuple[str, str]:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or u.hostname not in self.allowed or u.username or u.password:
            raise ValueError("refused URL outside the public-data allow-list")
        return u.hostname, (u.path or "/") + (("?" + u.query) if u.query else "")

    def _connect(self, host: str) -> http.client.HTTPSConnection:
        if self._ctx is None:
            self._ctx, self.tls_note = tls_context()
        return http.client.HTTPSConnection(host, timeout=self.timeout, context=self._ctx)

    def _redirect(self, url: str, status: int, location: Optional[str]) -> str:
        """A redirect is followed only to an https URL on an allow-listed host (at most
        MAX_REDIRECTS hops); anything else is a FetchError that names the target."""
        if not location:
            raise FetchError(status, f"HTTP {status} redirect without a Location header", url)
        target = urllib.parse.urljoin(url, location)
        try:
            self._check(target)
        except ValueError:
            raise FetchError(status, f"HTTP {status} redirect to {target} refused "
                                     f"(not https or host not in the allow-list)", url)
        return target

    def get(self, url: str, start: Optional[int] = None, end: Optional[int] = None, _hops: int = 0) -> bytes:
        host, path = self._check(url)
        headers = {"User-Agent": f"fb-lwo-w3/{__version__}", "Accept-Encoding": "identity"}
        if start is not None:
            headers["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        last = None
        for attempt in range(self.retries + 1):
            conn = self._conns.get(host)
            try:
                if conn is None:
                    conn = self._connect(host)
                    self._conns[host] = conn
                conn.request("GET", path, headers=headers)
                r = conn.getresponse()
                body = r.read()
                self.requests += 1
                self.bytes += len(body)
                if self.bytes > self.max_bytes:
                    raise DownloadCapExceeded(f"downloaded {self.bytes} bytes > cap {self.max_bytes}")
                if r.status in (200, 206):
                    if start is not None and r.status == 200 and start > 0:
                        body = body[start:None if end is None else end + 1]
                    return body
                if r.status in REDIRECT_CODES:
                    target = self._redirect(url, r.status, r.getheader("Location"))
                    if _hops >= MAX_REDIRECTS:
                        raise FetchError(r.status, f"more than {MAX_REDIRECTS} redirects", url)
                    return self.get(target, start, end, _hops + 1)
                if r.status in (403, 404, 416):
                    raise FetchError(r.status, url=url)
                last = FetchError(r.status, url=url)
            except FetchError:
                raise
            except ssl.SSLCertVerificationError as e:
                self._conns.pop(host, None)   # not retried: it will not fix itself
                raise FetchError(-1, f"TLS certificate verification failed for {host}: "
                                     f"{getattr(e, 'verify_message', '') or e} (trust: {self.tls_note})"[:300], url)
            except (OSError, http.client.HTTPException) as e:
                last = FetchError(-1, f"{type(e).__name__}: {e}"[:300], url)
                self._conns.pop(host, None)
            time.sleep(min(8, 2 ** attempt))
        raise last or FetchError(-1, "no response", url)

    def probe(self, url: str) -> Tuple[bool, int, str]:
        """GET of byte 0 only (the engine never sends HEAD or any other method).
        Returns (published, HTTP status or -1, reason)."""
        try:
            self.get(url, 0, 0)
            return True, 206, "published"
        except FetchError as e:
            return False, e.status, e.reason

    def exists(self, url: str) -> bool:
        return self.probe(url)[0]

    def close(self):
        for c in self._conns.values():
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()

# --------------------------------------------------------------------------
# Decoder interface
# --------------------------------------------------------------------------


@dataclass
class Grid:
    values: "np.ndarray"        # flat float64, NaN where missing
    grid_id: str
    lats: Optional["np.ndarray"] = None
    lons: Optional["np.ndarray"] = None


class Decoder:
    """Decode one GRIB2 message (bytes) to a flat value array plus its grid."""

    def decode(self, buf: bytes, need_latlon: bool) -> Grid:  # pragma: no cover - interface
        raise NotImplementedError


# ecCodes settings that would make the library write files or switch to eckit's geo code (which
# has its own temp/cache facilities). They are removed from the environment before ecCodes is
# imported, so GRIB stays in memory: codes_new_from_message() parses the bytes in place.
ECCODES_WRITE_ENV = ("ECCODES_GRIB_WRITE_ON_FAIL", "ECCODES_GRIB_DUMP_JPG_FILE", "ECCODES_ECKIT_GEO",
                     "ECCODES_LOG_STREAM", "ECCODES_DEBUG")


class EccodesDecoder(Decoder):
    def __init__(self):
        for k in ECCODES_WRITE_ENV:
            os.environ.pop(k, None)
        import eccodes  # noqa: F401  (import error -> clear message from main)
        self.ec = eccodes

    def decode(self, buf: bytes, need_latlon: bool) -> Grid:
        import numpy as np
        ec = self.ec
        h = ec.codes_new_from_message(buf)
        try:
            vals = np.asarray(ec.codes_get_values(h), dtype="float64")
            if ec.codes_get(h, "bitmapPresent"):
                vals[vals == ec.codes_get(h, "missingValue")] = np.nan
            gid = "{}:{}:{}:{}".format(ec.codes_get(h, "gridType"), ec.codes_get(h, "numberOfDataPoints"),
                                       ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"),
                                       ec.codes_get(h, "longitudeOfFirstGridPointInDegrees"))
            lats = lons = None
            if need_latlon:
                lats = np.asarray(ec.codes_get_array(h, "latitudes"), dtype="float64")
                lons = np.asarray(ec.codes_get_array(h, "longitudes"), dtype="float64")
            return Grid(vals, gid, lats, lons)
        finally:
            ec.codes_release(h)

# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def gc_nmi(lat1, lon1, lat2, lon2):
    """Great-circle distance in nmi (numpy-friendly)."""
    import numpy as np
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R_NMI * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    import numpy as np
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    x = np.sin(dl) * np.cos(p2)
    y = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return (np.degrees(np.arctan2(x, y)) + 360.0) % 360.0


@dataclass
class SiteGeom:
    pad: "np.ndarray"   # nearest grid index per pad
    r5: "np.ndarray"
    r10: "np.ndarray"
    r60: "np.ndarray"
    dist60: "np.ndarray"   # nmi from the pad centroid, aligned with r60
    brg60: "np.ndarray"    # bearing from the pad centroid, aligned with r60
    covered: bool          # every pad is inside the grid
    ring10: "np.ndarray" = None   # > 5 and <= 10 nmi (r10 minus r5), for the 4.1.3.3 tier


def site_geometry(site: dict, lats, lons, search_nmi: float) -> SiteGeom:
    """Points within great-circle nmi circles around EACH pad (union). The nearest
    grid point to each pad is always included, so coarse grids (0.25 deg ~ 15 nmi)
    still have a pad column inside the 5 nmi set (near-pad simplification)."""
    import numpy as np
    lons = ((lons + 180.0) % 360.0) - 180.0
    clat = float(np.mean([p["lat"] for p in site["pads"]]))
    clon = float(np.mean([p["lon"] for p in site["pads"]]))
    # cheap prefilter box around the site
    box = (np.abs(lats - clat) < 2.5) & (np.abs(((lons - clon + 180) % 360) - 180) < 3.0)
    cand = np.nonzero(box)[0]
    if cand.size == 0:
        e = np.zeros(0, dtype=int)
        return SiteGeom(e, e, e, e, np.zeros(0), np.zeros(0), False, e)
    pads, dmin = [], np.full(cand.size, np.inf)
    covered = True
    for p in site["pads"]:
        d = gc_nmi(p["lat"], p["lon"], lats[cand], lons[cand])
        j = int(np.argmin(d))
        if d[j] > 20.0:
            covered = False
        pads.append(cand[j])
        dmin = np.minimum(dmin, d)
    pad = np.array(sorted(set(int(x) for x in pads)), dtype=int)

    def ring(r):
        return np.union1d(cand[dmin <= r], pad)
    r60 = ring(search_nmi)
    r5, r10 = ring(5.0), ring(10.0)
    return SiteGeom(pad, r5, r10, r60,
                    gc_nmi(clat, clon, lats[r60], lons[r60]),
                    bearing_deg(clat, clon, lats[r60], lons[r60]), covered,
                    np.setdiff1d(r10, r5))

# --------------------------------------------------------------------------
# Column physics (ours) and the five 4010B rule proxies
# --------------------------------------------------------------------------


@dataclass
class Layer:
    base_z: float
    top_z: float
    base_t: float
    top_t: float
    base_p: float
    top_p: float
    clw_max: Optional[float]

    @property
    def depth(self):
        return self.top_z - self.base_z


@dataclass
class Column:
    p: List[float]           # hPa, descending (surface first)
    z: List[float]           # m
    t: List[float]           # deg C
    rh: List[float]          # %
    clw: Dict[float, float] = field(default_factory=dict)  # hPa -> kg/kg (where known)
    cape: Optional[float] = None
    refc: Optional[float] = None
    retop: Optional[float] = None   # echo top, m
    precip_in_h: Optional[float] = None
    conv_precip: Optional[bool] = None
    brightband: Optional[bool] = None


def _interp(x, x0, x1, y0, y1):
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def cloud_layers(c: Column, rh_min: float) -> List[Layer]:
    """Contiguous model levels with RH >= rh_min (ours). Bounds extend half-way to the
    neighbouring clear levels, so a 2-level layer has a finite depth."""
    n = len(c.p)
    out, i = [], 0
    while i < n:
        if c.rh[i] >= rh_min:
            j = i
            while j + 1 < n and c.rh[j + 1] >= rh_min:
                j += 1
            bz = c.z[i] if i == 0 else 0.5 * (c.z[i] + c.z[i - 1])
            bt = c.t[i] if i == 0 else 0.5 * (c.t[i] + c.t[i - 1])
            tz = c.z[j] if j == n - 1 else 0.5 * (c.z[j] + c.z[j + 1])
            tt = c.t[j] if j == n - 1 else 0.5 * (c.t[j] + c.t[j + 1])
            known = [c.clw[c.p[k]] for k in range(i, j + 1) if c.p[k] in c.clw]
            clw = max(known) if known and len(known) == j - i + 1 else None
            out.append(Layer(bz, tz, bt, tt, c.p[i], c.p[j], clw))
            i = j + 1
        else:
            i += 1
    return out


def lcl_height(c: Column) -> float:
    """LCL above the lowest level (Espy, 125 m per deg C of dewpoint depression; ours)."""
    t, rh = c.t[0], max(1.0, min(100.0, c.rh[0]))
    a, b = 17.62, 243.12
    g = math.log(rh / 100.0) + a * t / (b + t)
    td = b * g / (a - g)
    return c.z[0] + 125.0 * max(0.0, t - td)


def cumulus_tops(c: Column, R) -> List[float]:
    """Top temperatures of model clouds identified as cumulus (CAPE >= 100 J/kg and a
    cloud layer based near the LCL; ours). Anvil-level layers are not cumulus here."""
    if c.cape is None or c.cape < R["cuCapeMin"]:
        return []
    lcl = lcl_height(c)
    return [L.top_t for L in cloud_layers(c, R["cloudRh"]) if L.base_z <= lcl + R["cuBaseTolM"]]


def top_temp(c: Column, R) -> Optional[float]:
    """Coldest cloud-top temperature in the column (highest RH layer), else None."""
    ls = cloud_layers(c, R["cloudRh"])
    return min(L.top_t for L in ls) if ls else None


def temp_at_height(c: Column, zq: float) -> Optional[float]:
    for k in range(len(c.z) - 1):
        if c.z[k] <= zq <= c.z[k + 1]:
            return _interp(zq, c.z[k], c.z[k + 1], c.t[k], c.t[k + 1])
    if c.z and zq > c.z[-1]:
        return c.t[-1]
    return None


def is_core(c: Column, coarse: bool, R) -> bool:
    if not coarse:
        return c.refc is not None and c.refc >= R["coreRefcDbz"]
    return bool(c.cape is not None and c.cape >= R["coreCapeCoarse"] and c.conv_precip)


def parent_top_cold(c: Column, R) -> bool:
    """Parent top <= -10 C: echo top temperature if the model has RETOP, else cloud top."""
    temps = []
    if c.retop is not None and c.retop > 0:
        tt = temp_at_height(c, c.retop)
        if tt is not None:
            temps.append(tt)
    ct = top_temp(c, R)
    if ct is not None:
        temps.append(ct)
    return bool(temps) and min(temps) <= R["anvilTopC"]


def anvil_cloud(c: Column, R) -> bool:
    return any(R["anvilPMin"] <= p <= R["anvilPMax"] and rh >= R["cloudRh"] for p, rh in zip(c.p, c.rh))


def vote_cu(pad: Sequence[Column], r5: Sequence[Column], ring10: Sequence[Column], R) -> bool:
    """4010B 4.1.3 (LLCCR 9-11), proxy. Three tiers:
      (1) top <= -5 C over a pad column (4.1.3.1.b, flight through; near-pad proxy);
      (2) top <= -10 C within the 5 nmi ring (4.1.3.2, slant > 0 and <= 5 nmi);
      (3) top <= -20 C in the > 5 and <= 10 nmi ring only (4.1.3.3), not the whole 0-10 nmi disc.
    Not modeled (no vote): 4.1.3.1.a (top <= +5 C and warmer than -5 C) and its three
    shallow-cumulus exception paths 4.1.3.1.a(1)-(3). A top at exactly -5 C is 4.1.3.1.b."""
    if any(t <= R["cuPadTopC"] for c in pad for t in cumulus_tops(c, R)):
        return True
    if any(t <= R["cu5TopC"] for c in r5 for t in cumulus_tops(c, R)):
        return True
    return any(t <= R["cu10TopC"] for c in ring10 for t in cumulus_tops(c, R))


def _thick(L: Layer, R) -> bool:
    # any part between 0 C and -20 C, inclusive; >= 1.4 km (4,500 ft) thick
    warm, cold = max(L.base_t, L.top_t), min(L.base_t, L.top_t)
    return L.depth >= R["thickMinM"] and cold <= R["thickWarmC"] and warm >= R["thickColdC"]


def _cirriform_exempt(L: Layer, col: Column, core_near: bool, R) -> bool:
    """4010B 4.1.8.2 (LLCCR 23) exception, by proxy. Wording (Flight Desk 4010B read, 2026-11-17):
    the layer and any connected thick layer is cirriform, has never been connected to convective
    clouds, is entirely <= -15 C, and shows no evidence of liquid water. Proxy of each term:
      * entirely <= -15 C: both layer bounds <= cirriformMaxC (4010B number);
      * no evidence of liquid water: cloud-water mixing ratio (CLMR) known at EVERY layer level and
        <= clwZero (ours). Unknown CLW (ECMWF, or a layer below 500 hPa) -> NOT exempt;
      * never connected to convective clouds: no core within 10 nmi now and column CAPE < cuCapeMin
        (ours; the engine has no cloud history, so "never" is only "not now");
      * cirriform: no model gives cloud type, so it is taken as the two tests above (ice-only and
        cold). Connected layers in the column are already merged, so the test covers them."""
    if L.clw_max is None:
        return False
    never_convective = not core_near and (col.cape is None or col.cape < R["cuCapeMin"])
    return (never_convective and max(L.base_t, L.top_t) <= R["cirriformMaxC"]
            and L.clw_max <= R["clwZero"])


def _is_cumulus_layer(L: Layer, c: Column, R) -> bool:
    return c.cape is not None and c.cape >= R["cuCapeMin"] and L.base_z <= lcl_height(c) + R["cuBaseTolM"]


def _precip_connects(c: Column, R) -> bool:
    """Column precipitation that physically connects layers. ours (proxy): composite REFC >=
    precipConnectDbz or 1-h surface precip >= precipConnectIn. Limitation: the engine fetches only
    composite (column-max) reflectivity, not reflectivity at levels, so it cannot test the gap
    between layers; virga or an elevated echo can over-connect. Missing fields -> no connection."""
    return bool((c.refc is not None and c.refc >= R["precipConnectDbz"]) or
                (c.precip_in_h is not None and c.precip_in_h >= R["precipConnectIn"]))


def thick_layers(c: Column, core_near: bool, R) -> List[Layer]:
    """Layers of one column for the NASA-STD-4010B 4.1.8 test (read from the 4010B PDF, Final
    08-27-2026, p. 19; 3.2 "Thick Cloud Layer"), after the 4.1.8 lead-in rules:
      * anvil-level layers near a core are left to the anvil proxies (not thick cloud);
      * vertically continuous cloud is one layer (contiguous RH levels);
      * layers connected by precipitation are combined into one (base of the lowest to top
        of the highest), when the column has precipitation (ours: REFC >= 15 dBZ or >= 0.01 in/h);
      * a cumulus is never combined with layered clouds ("a cumulus cloud is not combined with
        cloud layers to increase the total thickness"; 4.3.c: assessed independently). The
        smoke-cumulus exception (4.1.8 lead-in a/b, 4.3.d: attached to its plume, or detached
        <= 60 min) is NOT MODELED: no model has smoke.
    Precipitation only ever ADDS a combination: without it the vertically continuous layers are
    returned unchanged, so the base thick-layer test never needs precipitation.
    The cirriform exception (4.1.8.2, by proxy) is applied to the resulting layers."""
    raw = []
    for L in cloud_layers(c, R["cloudRh"]):
        if core_near and L.base_p <= R["anvilPMax"]:
            continue
        raw.append(L)
    if _precip_connects(c, R) and len(raw) > 1:
        merged: List[Layer] = []
        for L in raw:
            if merged and not _is_cumulus_layer(L, c, R) and not _is_cumulus_layer(merged[-1], c, R):
                P = merged[-1]
                clw = None if P.clw_max is None or L.clw_max is None else max(P.clw_max, L.clw_max)
                merged[-1] = Layer(P.base_z, L.top_z, P.base_t, L.top_t, P.base_p, L.top_p, clw)
            else:
                merged.append(L)
        raw = merged
    return [L for L in raw if not _cirriform_exempt(L, c, core_near, R)]


def vote_thick(r5: Sequence[Column], core_near: bool, R) -> bool:
    """4010B 4.1.8 (LLCCR 22-24), proxy. Yes if any column in the 5 nmi ring (near-pad proxy
    of slant <= 5 nmi from the flight path) has a layer >= 1.4 km thick with any part between
    0 C and -20 C inclusive (4.1.8.1.a). A layer connected to such a layer in the ring
    (4.1.8.1.b) is covered by the same ring test. Layers are combined per the 4.1.8 lead-in
    (see thick_layers). The 4.1.8.2 cirriform exception is applied by proxy (_cirriform_exempt).
    Not modeled (exception never applied, so the vote can only be conservative): the 4.1.8.3
    reflectivity exceptions (0 dBZ
    within 5 nmi; MRR >= +7.5 dBZ at a horizontal distance <= 2 nmi in the last hour) and
    smoke-cumulus combining."""
    return any(_thick(L, R) for c in r5 for L in thick_layers(c, core_near, R))


def vote_att(r10: Sequence[Column], coarse: bool, R) -> bool:
    """4010B 4.1.4 (LLCCR 12-14), proxy: "attached anvil likely within 10 nmi" = a core,
    a parent top/echo top <= -10 C (4.1.4 lead-in) and anvil-level cloud, all within 10 nmi.
    Exemptions are never applied (they are not triggers and not modeled):
      * 'entirely colder than 0 C' (4.1.4.1, 4.1.4.2, 4.1.4.3);
      * MRR < +7.5 dBZ at <= 1 nmi horizontal, which is an exemption ONLY in 4.1.4.1.
    4.1.4.1 (through or slant <= 3 nmi) is shall-not-launch unless BOTH hold, and has NO wait
    clock; the engine has no wait clock on any band (the retired App. G 3-h wait is not used).
    Not modeled: 4.1.4.2 (3 h at > 3 and <= 5 nmi) and 4.1.4.3 (30 min at > 5 and <= 10 nmi)."""
    cores = [c for c in r10 if is_core(c, coarse, R)]
    return any(parent_top_cold(c, R) for c in cores) and any(anvil_cloud(c, R) for c in r10)


def vote_det(r10: Sequence[Column], r60: Sequence[Column], dist60, brg60, u250: Optional[float],
             v250: Optional[float], coarse: bool, R) -> bool:
    """4010B 4.1.5 (LLCCR 15-17), proxy: anvil-level cloud within 10 nmi with no local core,
    and a core with parent top <= -10 C (4.1.5 lead-in) upwind within the 60 nmi storm search
    (ours), reachable within 2 h on the 250 hPa wind (ours). Exemptions are never applied:
    MRR < +7.5 dBZ is an exemption only in 4.1.5.1 and 4.1.5.2.a; 4.1.5.3 uses 0 C only.
    Not modeled: the 4.1.5 wait periods and the 4.1.5.2.b field-mill / +10 dBZ path."""
    if u250 is None or v250 is None or not any(anvil_cloud(c, R) for c in r10):
        return False
    if any(is_core(c, coarse, R) for c in r10):
        return False
    spd_kt = math.hypot(u250, v250) * MS_TO_KT
    reach = min(R["detSearchNmi"], spd_kt * R["detTravelH"])
    wind_from = (math.degrees(math.atan2(u250, v250)) + 180.0) % 360.0
    for c, d, b in zip(r60, dist60, brg60):
        if d > reach or not is_core(c, coarse, R) or not parent_top_cold(c, R):
            continue
        diff = abs((b - wind_from + 180.0) % 360.0 - 180.0)
        if diff <= R["detUpwindDeg"]:
            return True
    return False


def vote_dist(r5: Sequence[Column], R) -> bool:
    """4010B 4.1.7 (LLCCR 21) with 4.2.2.b, proxy: cloud tops colder than 0 C with moderate or
    greater precipitation (REFC >= 30 dBZ, 4.2.2.b(3); or > 0.10 in in 1 h, 4.2.2.b(2), the
    1-h accumulation is ours) or a bright band, within the 5 nmi ring.
    Not modeled (no vote): the 4.2.2.b(1) visual / any-amount-of-hail path, and the 3.2
    Disturbed Weather exclusions (sea/land breeze, frictional convergence, outflow boundaries);
    there is no weather-system classifier."""
    for c in r5:
        tt = top_temp(c, R)
        if tt is None or not tt < R["distTopC"]:
            continue
        if (c.refc is not None and c.refc >= R["distRefcDbz"]) or \
           (c.precip_in_h is not None and c.precip_in_h > R["distPrecipIn"]) or c.brightband:
            return True
    return False


# --------------------------------------------------------------------------
# Model-hour field assembly
# --------------------------------------------------------------------------


@dataclass
class HourFields:
    """Decoded fields for one model-hour: name -> {level or None: flat values}."""
    data: Dict[str, Dict[Optional[int], "np.ndarray"]]
    lats: "np.ndarray"
    lons: "np.ndarray"
    grid_id: str


def to_canonical(model_id: str, f: str, vals):
    """Normalise units: deg C, %, m, m/s, mm, J/kg, dBZ."""
    if f == "t":
        return vals - 273.15
    if model_id in ("ecmwf", "ecmwf-ens") and f in ("apcp", "cpacc"):
        return vals * 1000.0   # m -> mm
    return vals


def columns_at(h: HourFields, idx, model_id: str, precip_mm_h, conv) -> List[Column]:
    import numpy as np
    d = h.data
    levels = sorted([p for p in d.get("t", {}) if p in d.get("rh", {}) and p in d.get("z", {})], reverse=True)
    out = []
    for i in idx:
        ps, zs, ts, rs = [], [], [], []
        for p in levels:
            t, r, z = d["t"][p][i], d["rh"][p][i], d["z"][p][i]
            if np.isfinite(t) and np.isfinite(r) and np.isfinite(z):
                ps.append(float(p)); zs.append(float(z)); ts.append(float(t)); rs.append(float(r))
        clw = {float(p): float(a[i]) for p, a in d.get("clw", {}).items() if np.isfinite(a[i])}

        def s(name):
            a = d.get(name, {}).get(None)
            if a is None or not np.isfinite(a[i]):
                return None
            return float(a[i])
        pr = None if precip_mm_h is None or not np.isfinite(precip_mm_h[i]) else float(precip_mm_h[i]) / MM_PER_IN
        cv = None if conv is None else bool(conv[i])
        out.append(Column(ps, zs, ts, rs, clw, s("cape"), s("refc"), s("retop"), pr, cv, None))
    return out


@dataclass
class SiteHour:
    votes: Dict[str, bool]
    ltg: Optional[bool]
    gust_kt: Optional[float]
    pw_mm: Optional[float]
    shear_kt: Optional[float]


class AccTracker:
    """Turns accumulations over [a, b] into a 1-h-ish rate (mm/h), using the previous
    accumulation with the same start when the window is longer than 1 h."""

    def __init__(self):
        self.prev: Dict[Tuple[str, int], Tuple[int, "np.ndarray"]] = {}

    def rate(self, key: str, a: int, b: int, acc):
        import numpy as np
        if b <= a:
            return None
        p = self.prev.get((key, a))
        self.prev[(key, a)] = (b, acc)
        if b - a == 1:
            return acc
        if p is not None and p[0] < b:
            return np.maximum(0.0, (acc - p[1]) / (b - p[0]))
        return acc / (b - a)


def evaluate_site_hour(spec: ModelSpec, h: HourFields, g: SiteGeom, R, precip, conv) -> Optional[SiteHour]:
    import numpy as np
    if not g.covered:
        return None
    d = h.data

    def smax(name, idx, scale=1.0):
        a = d.get(name, {}).get(None)
        if a is None or idx.size == 0:
            return None
        v = a[idx]
        v = v[np.isfinite(v)]
        return None if v.size == 0 else float(v.max()) * scale

    gust = smax("gust", g.r10, MS_TO_KT)
    if not spec.votes:
        return SiteHour({}, None, gust, None, None)
    have_cols = len([p for p in d.get("t", {}) if p in d.get("rh", {}) and p in d.get("z", {})]) >= 3
    required = ["cape"] + ([] if spec.coarse else ["refc"]) + (["ltng"] if spec.ltg else [])
    if not have_cols or any(None not in d.get(r, {}) for r in required):
        return None
    cols60 = columns_at(h, g.r60, spec.id, precip, conv)
    pos = {int(i): k for k, i in enumerate(g.r60)}
    pick = lambda idx: [cols60[pos[int(i)]] for i in idx]
    pad, r5, r10 = pick(g.pad), pick(g.r5), pick(g.r10)
    core_near = any(is_core(c, spec.coarse, R) for c in r10)

    def padmean(name, lev=None):
        a = d.get(name, {}).get(lev)
        if a is None:
            return None
        v = a[g.pad]
        v = v[np.isfinite(v)]
        return None if v.size == 0 else float(v.mean())
    u250, v250 = padmean("u", 250), padmean("v", 250)
    votes = {
        "cu": vote_cu(pad, r5, pick(g.ring10), R),
        "thick": vote_thick(r5, core_near, R),
        "att": vote_att(r10, spec.coarse, R),
        "det": vote_det(r10, cols60, g.dist60, g.brg60, u250, v250, spec.coarse, R),
        "dist": vote_dist(r5, R),
    }
    ltg = None
    if spec.ltg:
        lt = smax("ltng", g.r10)
        ltg = lt is not None and lt > 0
    shear = None
    u9, v9, u5, v5 = padmean("u", 925), padmean("v", 925), padmean("u", 500), padmean("v", 500)
    if None not in (u9, v9, u5, v5):
        shear = math.hypot(u5 - u9, v5 - v9) * MS_TO_KT   # 925-500 hPa bulk shear (ours)
    return SiteHour(votes, ltg, gust, padmean("pwat"), shear)

# --------------------------------------------------------------------------
# Run selection and per-model processing
# --------------------------------------------------------------------------


def fmt_url(tpl: str, run: dt.datetime, fh: int) -> str:
    return tpl.format(ymd=run.strftime("%Y%m%d"), hh=run.strftime("%H"), ff=f"{fh:02d}",
                      fff=f"{fh:03d}", fh=fh, ymdhh=run.strftime("%Y%m%d%H"))


def fh_cap(run: dt.datetime, t0: dt.datetime) -> int:
    """Largest forecast hour that is still inside the window: valid time t0 + HOURS, so
    f(HOURS + cycle age); never past MAX_LEAD_H (f072)."""
    age = int((t0 - run).total_seconds() // 3600)
    return max(0, min(MAX_LEAD_H, HOURS + age))


def needed_steps(spec: ModelSpec, run: dt.datetime, t0: dt.datetime, hours: int, horizon: int) -> List[Tuple[int, int]]:
    """(fh, hour index) pairs for valid times in [t0, t0 + hours): capped by valid time (A), by the
    model's native horizon, and never past f072 (MAX_LEAD_H)."""
    off = int((run - t0).total_seconds() // 3600)
    cap = min(horizon, fh_cap(run, t0))
    out = []
    for fh in spec.steps(cap):
        i = off + fh
        if fh <= cap and 0 <= i < min(hours, HOURS):
            out.append((fh, i))
    return out


def _keep_step(sched: dict, fh: int, i: int) -> bool:
    x = fh if sched.get("basis", "board") == "fh" else i
    for seg in sched["segments"]:
        if x <= int(seg["toH"]):
            return fh % int(seg["stepH"]) == 0
    return False


def schedule_steps(spec: ModelSpec, run: dt.datetime, t0: dt.datetime, hours: int, horizon: int,
                   sched: Optional[dict] = None) -> List[Tuple[int, int]]:
    """needed_steps thinned to the fetch schedule (default: hourly NOW..+7 h, then 3-hourly to
    t0 + 48 h), still capped at the model's horizon and fh_cap (valid time, at most f072)."""
    sched = sched or DEFAULT_SCHEDULE
    out = [(fh, i) for fh, i in needed_steps(spec, run, t0, hours, horizon) if _keep_step(sched, fh, i)]
    if out and out[0][1] > 0:
        # Lead-in bracket: the last kept step before t0, so NOW is filled from two real steps of
        # this cycle (never extrapolated). It is used for the fill only and never published.
        off = int((run - t0).total_seconds() // 3600)
        before = [fh for fh in spec.steps(min(horizon, fh_cap(run, t0)))
                  if off + fh < 0 and fh < out[0][0] and _keep_step(sched, fh, off + fh)]
        if before:
            out.insert(0, (before[-1], off + before[-1]))
    last = min(hours, HOURS) - 1
    if out and out[-1][1] < last:
        # Lead-out bracket (the mirror of the lead-in): a 3-hourly grid can end 1-2 h before the
        # last output hour, so the first native step at or after it (valid time at most t0 + 50 h,
        # never past the native horizon or f072) is fetched too. Like the lead-in it only fills
        # the hours up to the window end and is never published itself.
        off = int((run - t0).total_seconds() // 3600)
        nat = min(spec.horizon_for(run.hour), MAX_LEAD_H)
        after = [fh for fh in spec.steps(nat)
                 if fh > out[-1][0] and last <= off + fh <= last + LEAD_OUT_MAX_H]
        if after:
            out.append((after[0], off + after[0]))
    return out


def select_run(spec: ModelSpec, fetcher: Fetcher, now: dt.datetime, t0: dt.datetime, hours: int,
               max_age_h: float, lookback_h: int = PROBE_LOOKBACK_H, sched: Optional[dict] = None,
               probes: Optional[list] = None) -> Tuple[Optional[dt.datetime], str]:
    """The LATEST PUBLISHED cycle only: walking back one cycle at a time (6 h: every model uses
    00/06/12/18Z) for at most `lookback_h` (24 h), the newest run whose last scheduled
    forecast-hour index exists (probed with a 1-byte GET) is used. A newer cycle that is not
    published yet is skipped, never mixed or time-lagged. Every probe is appended to `probes`
    as (url, status, reason). Returns (run, 'ok'|'stale'|'missing')."""
    base = now.replace(minute=0, second=0, microsecond=0)
    for back in range(0, lookback_h + 1):
        run = base - dt.timedelta(hours=back)
        if run.hour not in spec.cycles:
            continue
        hz = min(spec.horizon_for(run.hour), fh_cap(run, t0))
        steps = (schedule_steps(spec, run, t0, hours, hz, sched) if sched
                 else needed_steps(spec, run, t0, hours, hz))
        if not steps:
            continue
        last = steps[-1][0]
        ok = True
        for f in spec.files:
            url = fmt_url(f.index, run, last)
            pub, status, reason = fetcher.probe(url)
            if probes is not None:
                probes.append((url, status, reason))
            if not pub:
                ok = False
                break
        if ok:
            age = (now - run).total_seconds() / 3600
            return run, ("ok" if age <= max_age_h else "stale")
    return None, "missing"


def probe_summary(probes: list, ok_last: bool = False) -> str:
    """'url -> status reason' for each failed probe (the successful last one is left out)."""
    bad = probes[:-1] if ok_last else probes
    return "; ".join(f"{u} -> {st if st > 0 else 'no HTTP status'} ({r})" for u, st, r in bad) or "none"


def missing_reason(probes: list, lookback_h: int = PROBE_LOOKBACK_H) -> str:
    if not probes:
        return f"no cycle in the last {lookback_h} h has a step in the window"
    if all(st == 404 for _, st, _ in probes):
        return f"no cycle published in the last {lookback_h} h (every probe HTTP 404)"
    return f"no published cycle reachable in the last {lookback_h} h (transport, TLS, redirect or HTTP errors)"


@dataclass
class ModelResult:
    spec: ModelSpec
    run: Optional[dt.datetime]
    status: str
    max_h: int
    max_age_h: float
    per_site: Dict[str, Dict[int, SiteHour]]   # site id -> hour index -> values
    carried: bool = False                      # newest cycle unchanged: values come from the last output


# --------------------------------------------------------------------------
# Skip unchanged models (Lead 2026-11-17): a model whose newest cycle is the one the last good
# output already used is not fetched again; its votes are carried over from that output.
# --------------------------------------------------------------------------


def config_fingerprint(cfg: dict) -> str:
    """16 hex chars over everything that shapes the values (models, sites/pads, rules, max ages,
    ENS on/off, hours, schedule) plus the engine version. Published as cfgHash so the next run
    can tell whether the last output came from the same config."""
    keep = {k: cfg.get(k) for k in ("models", "sites", "rules", "maxAgeH", "exc", "hours", "schedule")}
    keep["engine"] = __version__
    blob = json.dumps(keep, sort_keys=True, separators=(",", ":"), default=list).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class CarrySource:
    doc: dict
    by: int            # whole hours from the previous output's t0 to this t0
    sites: Dict[str, dict]


def carry_source(prev: Optional[dict], cfg: dict, t0: dt.datetime, log) -> Optional[CarrySource]:
    """The last good output, if it can be carried from; otherwise None (every model is fetched).
    It must be readable, from the same config (cfgHash), not from the future, overlap this
    window, and still hold every configured site (a size-degraded file that dropped sites is not
    used)."""
    if prev is None:
        log("carry: no readable last good output; every model is fetched")
        return None
    try:
        if prev.get("cfgHash") != config_fingerprint(cfg):
            log("carry: last good output is from a different config; every model is fetched")
            return None
        hours = cfg["hours"]
        order = [m for m in MODEL_BIT_ORDER if m in cfg["models"]]
        by = int((t0 - parse_iso(prev["t0"])).total_seconds() // 3600)
        sites = {s["id"]: s for s in prev["sites"]}
        ok = (prev["modelOrder"] == order and prev["hours"] == hours and 0 <= by < hours
              and all(c["id"] in sites for c in cfg["sites"]))
        for c in cfg["sites"]:
            ps = sites.get(c["id"]) or {}
            ok = ok and all(len(m) == 2 * hours for m in [ps["avail"], ps["ltg"]] + list(ps["rules"].values()))
    except (KeyError, TypeError, ValueError, AttributeError):
        ok = False
    if not ok:
        log("carry: last good output cannot be carried from; every model is fetched")
        return None
    return CarrySource(prev, by, sites)


def carried_model(carry: Optional[CarrySource], mid: str, run: dt.datetime) -> Optional[dict]:
    """The last output's entry for this model if it used this same cycle with data (status ok)
    and reached the horizon these rules give that cycle at the last output's t0 (maxH). A file
    from older rules (e.g. the f048 cap: GFS 18Z maxH 48 instead of 55) is not carried, so no
    hours between its old end and the new tail are left uncovered."""
    if carry is None:
        return None
    m = next((x for x in carry.doc.get("models", []) if x.get("id") == mid), None)
    if not (m and m.get("run") == iso(run) and m.get("status") == "ok"):
        return None
    spec = MODELS.get(mid)
    try:
        want = min(spec.horizon_for(run.hour), fh_cap(run, parse_iso(carry.doc["t0"]))) if spec else None
    except (KeyError, TypeError, ValueError):
        return None
    return m if want is not None and m.get("maxH") == want else None


@dataclass
class CarriedSite:
    avail: List[int]
    rules: Dict[str, List[int]]
    ltg: List[int]
    gust: Dict[str, List[Optional[int]]]   # med/lo/hi, encoded kt, None where not carried
    pw: List[Optional[int]]                 # encoded hundredths of an inch
    shear: List[Optional[int]]              # 6-h blocks


def carry_site(carry: CarrySource, sid: str, carried_bits: int, gust_pure_ok: bool, hours: int) -> CarriedSite:
    """Shift the carried models' values from the last output to this t0 (valid time). Hours now
    in the past are dropped; hours past the old window are not computed for them (never invented).
    Vote/lightning/avail bits are per model, so they carry exactly. Gust/PW/shear are multi-model
    aggregates in v1, so they carry only for old hours whose aggregate came from carried models
    alone (pure); elsewhere they are left out (None)."""
    ps, by = carry.sites[sid], carry.by
    av, lt = decode_masks(ps["avail"]), decode_masks(ps["ltg"])
    ru = {r: decode_masks(ps["rules"][r]) for r in RULE_IDS}
    g = ps.get("gust") or {}
    old_n = len(av)

    def at(seq, j):
        return seq[j] if seq is not None and 0 <= j < len(seq) else None
    out = CarriedSite([0] * hours, {r: [0] * hours for r in RULE_IDS}, [0] * hours,
                      {"med": [None] * hours, "lo": [None] * hours, "hi": [None] * hours},
                      [None] * hours, [None] * math.ceil(hours / 6))
    pure = [((a & ~carried_bits) == 0) for a in av]
    for i in range(hours):
        j = i + by
        if j >= old_n:
            break
        out.avail[i] = av[j] & carried_bits
        out.ltg[i] = lt[j] & carried_bits
        for r in RULE_IDS:
            out.rules[r][i] = ru[r][j] & carried_bits
        if pure[j]:
            out.pw[i] = at(ps.get("pw"), j)
            if gust_pure_ok:
                for k in ("med", "lo", "hi"):
                    out.gust[k][i] = at(g.get(k), j)
    if by % 6 == 0:
        sb = by // 6
        old = (ps.get("shear") or {}).get("kt") or []
        for b in range(len(out.shear)):
            ob = b + sb
            hrs = range(ob * 6, min(old_n, ob * 6 + 6))
            if 0 <= ob < len(old) and hrs and all(pure[j] for j in hrs):
                out.shear[b] = old[ob]
    return out


def _cap_check(fetcher: Fetcher) -> None:
    if fetcher.bytes > fetcher.max_bytes:
        raise DownloadCapExceeded(f"downloaded {fetcher.bytes} bytes > cap {fetcher.max_bytes}")


def _get(fetcher: Fetcher, url: str, start: Optional[int] = None, end: Optional[int] = None) -> bytes:
    body = fetcher.get(url, start, end)
    _cap_check(fetcher)
    return body


def _want_bytes(wants: Sequence[Want], entries: Sequence[IdxEntry]) -> int:
    """Planned GRIB bytes for these messages. An open-ended last message (no next offset) is
    counted as the largest known message in the same index (conservative)."""
    known = [e.length for e in entries if e.end is not None]
    fallback = max(known) if known else 0
    return sum(w.entry.length if w.entry.end is not None else fallback for w in wants)


@dataclass
class StepPlan:
    fh: int
    i: int                                   # hour index from t0
    parts: List[Tuple[str, List[Want]]]      # (GRIB url, messages) per file
    nbytes: int                              # planned GRIB bytes

    @property
    def ranges(self) -> int:
        return sum(len(merge_ranges(w)) for _, w in self.parts)


def plan_model(spec: ModelSpec, fetcher: Fetcher, run: dt.datetime, steps: List[Tuple[int, int]]) -> List[StepPlan]:
    """Fetch the index of every scheduled step (one GET per file) and select its messages.
    No GRIB is downloaded here. A step whose index is missing is left out (no data, no fill)."""
    plans: List[StepPlan] = []
    for fh, i in steps:
        parts, nbytes, ok = [], 0, True
        for f in spec.files:
            try:
                text = _get(fetcher, fmt_url(f.index, run, fh)).decode("utf-8", "replace")
            except FetchError:
                ok = False
                break
            entries = parse_noaa_idx(text) if f.kind == "noaa" else parse_ecmwf_index(text)
            wants = (select_noaa(entries, f.fields, fh, spec.levels) if f.kind == "noaa"
                     else select_ecmwf(entries, f.fields, fh, spec.levels))
            parts.append((fmt_url(f.grib, run, fh), wants))
            nbytes += _want_bytes(wants, entries)
        if ok and any(w for _, w in parts):
            plans.append(StepPlan(fh, i, parts, nbytes))
    return plans


def fetch_hour(spec: ModelSpec, fetcher: Fetcher, decoder: Decoder, plan: StepPlan,
               grid_cache: Dict[str, Tuple["np.ndarray", "np.ndarray"]],
               model_key: Optional[str] = None) -> Optional[Tuple[HourFields, List[Want]]]:
    data: Dict[str, Dict[Optional[int], "np.ndarray"]] = {}
    wants_all: List[Want] = []
    gid = None
    for url, wants in plan.parts:
        for start, end, group in merge_ranges(wants):
            try:
                blob = _get(fetcher, url, start, end)
            except FetchError:
                return None
            for w in group:
                lo = w.entry.offset - start
                buf = blob[lo:] if w.entry.end is None else blob[lo:lo + w.entry.length]
                gr = decoder.decode(buf, need_latlon=False)
                if gr.grid_id not in grid_cache:
                    full = decoder.decode(buf, need_latlon=True)
                    grid_cache[gr.grid_id] = (full.lats, full.lons)
                gid = gid or gr.grid_id
                if gr.grid_id != gid:
                    continue  # mixed grids in one model-hour: keep the first grid only
                data.setdefault(w.field, {})[w.level] = to_canonical(model_key or spec.id, w.field, gr.values)
                wants_all.append(w)
            del blob
    if gid is None:
        return None
    lats, lons = grid_cache[gid]
    return HourFields(data, lats, lons, gid), wants_all


def merge_hours(a: SiteHour, b: SiteHour) -> SiteHour:
    """Conservative value for an hour between two fetched steps: a rule votes if EITHER
    neighbouring step votes; lightning likewise; gust / PW / shear take the larger value."""
    mx = lambda x, y: y if x is None else (x if y is None else max(x, y))
    votes = {r: bool(a.votes.get(r) or b.votes.get(r)) for r in set(a.votes) | set(b.votes)}
    ltg = None if a.ltg is None and b.ltg is None else bool(a.ltg or b.ltg)
    return SiteHour(votes, ltg, mx(a.gust_kt, b.gust_kt), mx(a.pw_mm, b.pw_mm), mx(a.shear_kt, b.shear_kt))


def fill_between_steps(per_site: Dict[int, SiteHour], scheduled: Sequence[int]) -> None:
    """Fill the hourly cells between two consecutive SCHEDULED steps that both produced data
    (merge_hours). No fill across a scheduled step that failed, before the first step, or
    after the last one (those hours stay uncovered: the model is not in n there)."""
    for a, b in zip(scheduled, scheduled[1:]):
        if b - a > 1 and a in per_site and b in per_site:
            m = merge_hours(per_site[a], per_site[b])
            for i in range(a + 1, b):
                per_site[i] = m


@dataclass
class ModelPrep:
    res: "ModelResult"
    plans: List[StepPlan]
    scheduled: List[int]
    hours: int = HOURS

    @property
    def nbytes(self) -> int:
        return sum(p.nbytes for p in self.plans)


def prepare_model(spec: ModelSpec, sites: List[dict], fetcher: Fetcher, now, t0, hours, max_age_h: float,
                  sched: dict, log, carry: Optional[CarrySource] = None) -> ModelPrep:
    probes: list = []
    run, status = select_run(spec, fetcher, now, t0, hours, max_age_h, sched=sched, probes=probes)
    res = ModelResult(spec, run, status, 0, max_age_h, {s["id"]: {} for s in sites})
    if status == "missing":
        log(f"model {spec.id}: missing: {missing_reason(probes)}; probes: {probe_summary(probes)}")
        return ModelPrep(res, [], [])
    if len(probes) > 1:
        log(f"model {spec.id}: newer cycles skipped: {probe_summary(probes, ok_last=True)}")
    if status != "ok":
        log(f"model {spec.id}: {status} (newest published cycle {run:%Y%m%d%H}, older than {max_age_h} h)")
        return ModelPrep(res, [], [])
    horizon = min(spec.horizon_for(run.hour), fh_cap(run, t0))
    res.max_h = horizon
    steps = schedule_steps(spec, run, t0, hours, horizon, sched)
    if carry is not None and carried_model(carry, spec.id, run) is None and any(
            x.get("id") == spec.id and x.get("run") == iso(run) and x.get("status") == "ok"
            for x in carry.doc.get("models", [])):
        log(f"model {spec.id}: newest cycle {run:%Y%m%d%H} unchanged, but the last output used a "
            f"different horizon for it (older rules); fetched again, not carried")
    if carried_model(carry, spec.id, run) is not None:
        # Same cycle as the last output: its votes are carried. With the valid-time cap (A) the
        # window end moved by `by` hours, so only the steps past the old window end are fetched
        # (plus the step just before them, so the hours in between are filled from this run).
        res.carried = True
        tail_from = hours - carry.by
        tail = [s for s in steps if s[1] >= tail_from]
        if tail:
            before = [s for s in steps if s[1] < tail_from]
            steps = ([before[-1]] if before else []) + tail
        else:
            steps = []
        if not steps:
            log(f"model {spec.id}: newest cycle {run:%Y%m%d%H} unchanged since the last output; "
                f"not fetched, votes carried (shift {carry.by} h)")
            return ModelPrep(res, [], [])
        plans = plan_model(spec, fetcher, run, steps)
        log(f"model {spec.id}: newest cycle {run:%Y%m%d%H} unchanged since the last output; votes "
            f"carried (shift {carry.by} h); fetching {len(plans)}/{len(steps)} steps past the old window "
            f"end, {sum(p.nbytes for p in plans)} bytes")
        return ModelPrep(res, plans, [i for _, i in steps], min(hours, HOURS))
    plans = plan_model(spec, fetcher, run, steps)
    log(f"model {spec.id}: run {run:%Y%m%d%H} plan {len(plans)}/{len(steps)} steps, "
        f"{sum(p.nbytes for p in plans)} bytes in {sum(p.ranges for p in plans)} ranges")
    return ModelPrep(res, plans, [i for _, i in steps], min(hours, HOURS))


def execute_model(prep: ModelPrep, sites: List[dict], fetcher: Fetcher, decoder: Decoder, R, log) -> "ModelResult":
    res, spec = prep.res, prep.res.spec
    if res.status != "ok" or (res.carried and not prep.plans):
        return res
    grid_cache: Dict[str, Tuple] = {}
    geoms: Dict[Tuple[str, str], SiteGeom] = {}
    acc = AccTracker()
    try:
        for plan in prep.plans:
            got = fetch_hour(spec, fetcher, decoder, plan, grid_cache)
            if got is None:
                continue
            h, wants = got
            win = {w.field: w.window for w in wants if w.field in ("apcp", "cpacc")}
            precip = None
            if "apcp" in h.data and None in h.data["apcp"]:
                a, b = win["apcp"]
                precip = acc.rate("apcp", a, b, h.data["apcp"][None])
            conv = None
            if "cprate" in h.data and None in h.data["cprate"]:
                conv = h.data["cprate"][None] > 0
            elif "cpacc" in h.data and None in h.data["cpacc"]:
                a, b = win["cpacc"]
                r = acc.rate("cpacc", a, b, h.data["cpacc"][None])
                conv = None if r is None else r > 0.01
            elif precip is not None:
                conv = precip > 0.1   # no convective field (GEFS): any measurable precip (ours)
            for s in sites:
                key = (h.grid_id, s["id"])
                if key not in geoms:
                    geoms[key] = site_geometry(s, h.lats, h.lons, R["detSearchNmi"])
                sh = evaluate_site_hour(spec, h, geoms[key], R, precip, conv)
                if sh is not None:
                    res.per_site[s["id"]][plan.i] = sh
            del h
    finally:
        # raw GRIB is only ever in memory; drop this model's buffers and grids now
        grid_cache.clear()
        geoms.clear()
        acc.prev.clear()
        prep.plans = []
    for s in sites:
        ph = res.per_site[s["id"]]
        fill_between_steps(ph, prep.scheduled)
        for i in [i for i in ph if i < 0 or i >= prep.hours]:
            del ph[i]          # the lead-in / lead-out bracket steps only fill the window's ends
    covered = sorted(set().union(*[set(v) for v in res.per_site.values()])) if res.per_site else []
    span = f"h{covered[0]}-h{covered[-1]}" if covered else "none"
    log(f"model {spec.id}: run {res.run:%Y%m%d%H} valid hours covered {len(covered)} ({span}); "
        f"site-hours {sum(len(v) for v in res.per_site.values())} over {len(res.per_site)} sites")
    return res


def run_model(spec: ModelSpec, sites: List[dict], fetcher: Fetcher, decoder: Decoder, now, t0, hours, R,
              max_age_h: float, log, sched: Optional[dict] = None) -> "ModelResult":
    prep = prepare_model(spec, sites, fetcher, now, t0, hours, max_age_h, sched or DEFAULT_SCHEDULE, log)
    return execute_model(prep, sites, fetcher, decoder, R, log)


@dataclass
class EnsResult:
    run: dt.datetime
    members: int        # perturbed members decoded (50 when every member is present)
    k: Dict[str, List[List[Optional[int]]]]   # site -> rows x blocks


def _ens_steps(h):
    """ENS steps: 6-hourly to h, where the caller passes min(native horizon, fh_cap) so the last
    step is at valid time t0 + 48 h at most (Phil 2026-11-17 8:14 PM CT, "A"). Each step fills the
    6-h block that holds fh - 3 h."""
    return list(range(6, min(h, MAX_LEAD_H) + 1, 6))


ENS_SPEC = ModelSpec("ecmwf-ens", "ECMWF ENS", True, False, False, (0, 6, 12, 18),
                     {0: 360, 12: 360, -1: 144}, 30, (ENS_FILE,), steps=_ens_steps)


@dataclass
class EnsPrep:
    run: Optional[dt.datetime]
    steps: List[Tuple[int, int, str, List[Want]]]   # (fh, block, GRIB url, messages)
    nbytes: int = 0
    carried: Optional["EnsResult"] = None            # same ENS cycle as the last output


def carried_ens(carry: Optional[CarrySource], run: dt.datetime, sites, hours, block_h=6) -> Optional["EnsResult"]:
    """The last output's member counts for this same ENS cycle, shifted to this t0's 6-h blocks.
    Only when the shift is a whole number of blocks and every site has them; else None."""
    if carry is None or carry.by % block_h:
        return None
    sb, nblk = carry.by // block_h, math.ceil(hours / block_h)
    k, members = {}, None
    for s in sites:
        e = (carry.sites.get(s["id"]) or {}).get("exc")
        if not e or e.get("run") != iso(run) or not e.get("k"):
            return None
        members = e.get("members")
        k[s["id"]] = [[(row[b + sb] if 0 <= b + sb < len(row) else None) for b in range(nblk)] for row in e["k"]]
    if not isinstance(members, int) or members < 1:
        return None
    return EnsResult(run, members, k)


def prepare_ens(fetcher: Fetcher, now, t0, hours, block_h=6, log=lambda m: None,
                carry: Optional[CarrySource] = None, sites=None) -> EnsPrep:
    """Latest published ENS cycle only (00/06/12/18Z), 6-hourly steps to valid time t0 + 48 h. One .index GET per step; every member's 10fg/tp message is byte-ranged from it."""
    probes: list = []
    run, _status = select_run(ENS_SPEC, fetcher, now, t0, hours, 30, probes=probes)
    if run is None:
        log(f"ens: missing: {missing_reason(probes)}; probes: {probe_summary(probes)}")
        return EnsPrep(None, [])
    if len(probes) > 1:
        log(f"ens: newer cycles skipped: {probe_summary(probes, ok_last=True)}")
    c = carried_ens(carry, run, sites or [], hours, block_h)
    nblk = math.ceil(hours / block_h)
    tail_blk = nblk - carry.by // block_h if c is not None else 0
    if c is not None and any(row[b] is None for rows in c.k.values() for row in rows for b in range(min(tail_blk, nblk))):
        # a carried block is empty (older rules ended earlier, or a step failed): fetch it all again
        log(f"ens: newest cycle {run:%Y%m%d%H} unchanged, but the last output has empty blocks; fetched again")
        c, tail_blk = None, 0
    if c is not None and tail_blk >= nblk:
        log(f"ens: newest cycle {run:%Y%m%d%H} unchanged since the last output; not fetched, "
            f"counts carried (shift {carry.by} h)")
        return EnsPrep(run, [], 0, c)
    horizon = min(ENS_SPEC.horizon_for(run.hour), fh_cap(run, t0) + block_h // 2)
    steps, total = [], 0
    for fh in ENS_SPEC.steps(horizon):
        b = int(((run + dt.timedelta(hours=fh - block_h / 2)) - t0).total_seconds() // (3600 * block_h))
        if b >= nblk:
            break
        if c is not None and b < tail_blk - 1:
            continue      # carried block; the one before the tail is kept for precip differencing
        try:
            text = _get(fetcher, fmt_url(ENS_FILE.index, run, fh)).decode("utf-8", "replace")
        except FetchError:
            steps.append((fh, b, "", []))   # a gap: the next block's precip cannot be differenced
            continue
        entries = parse_ecmwf_index(text)
        # The open-data "-ef" file holds the 50 PERTURBED members (type "pf", number 1-50) and
        # no control member (index checked 2026-11-17 00z 6h), so exc.members is 50, not 51.
        wants = [w for w in select_ecmwf(entries, ("gust", "apcp"), fh, ())
                 if w.entry.number is not None and w.entry.extra == "pf" and (w.field == "apcp" or b >= 0)]
        steps.append((fh, b, fmt_url(ENS_FILE.grib, run, fh), wants))
        total += _want_bytes(wants, entries)
    age = int((t0 - run).total_seconds() // 3600)
    if c is not None:
        log(f"ens: newest cycle {run:%Y%m%d%H} unchanged since the last output; counts carried "
            f"(shift {carry.by} h); fetching {len(steps)} steps for blocks {tail_blk}-{nblk - 1}, {total} bytes")
        return EnsPrep(run, steps, total, c)
    blocks = sorted({b for _, b, _, w in steps if w and 0 <= b < nblk})
    upto = min(hours, (blocks[-1] + 1) * block_h) if blocks else 0
    log(f"ens: run {run:%Y%m%d%H} (age {age} h at t0) plan {len(steps)} steps, {total} bytes; "
        f"covers {len(blocks)}/{nblk} 6-h blocks, through +{upto} h"
        + (f"; blocks after +{upto} h stay null (cycle horizon or a failed step)" if len(blocks) < nblk else ""))
    return EnsPrep(run, steps, total)


def execute_ens(prep: EnsPrep, sites, fetcher, decoder, t0, hours, block_h=6, log=lambda m: None) -> Optional[EnsResult]:
    """ECMWF ENS exceedance: member counts per 6-h block (never a percent)."""
    import numpy as np
    run = prep.run
    if prep.carried is not None and not prep.steps:
        return prep.carried
    if run is None:
        return None
    nblk = math.ceil(hours / block_h)
    gthr = (25, 30, 35, 40, 45)
    rthr = (0.10, 0.25, 0.50, 1.00)
    counts = {s["id"]: [[0] * nblk for _ in EXC_ROWS] for s in sites}
    seen = {s["id"]: [False] * nblk for s in sites}
    grid_cache: Dict[str, Tuple] = {}
    geoms: Dict[str, SiteGeom] = {}
    prev_tp: Dict[str, "np.ndarray"] = {}
    members = set()
    try:
        for fh, b, url, wants in prep.steps:
            if not url:
                prev_tp.clear()
                continue
            by_member: Dict[str, Dict[str, "np.ndarray"]] = {}
            lats = lons = None
            for start, end, group in merge_ranges(wants):
                try:
                    blob = _get(fetcher, url, start, end)
                except FetchError:
                    continue
                for w in group:
                    lo = w.entry.offset - start
                    buf = blob[lo:lo + w.entry.length] if w.entry.end is not None else blob[lo:]
                    gr = decoder.decode(buf, need_latlon=False)
                    if gr.grid_id not in grid_cache:
                        full = decoder.decode(buf, need_latlon=True)
                        grid_cache[gr.grid_id] = (full.lats, full.lons)
                    lats, lons = grid_cache[gr.grid_id]
                    by_member.setdefault(w.entry.number, {})[w.field] = to_canonical("ecmwf-ens", w.field, gr.values)
                del blob
            for m, fl in by_member.items():
                tp, ptp = fl.get("apcp"), prev_tp.get(m)
                if tp is not None:
                    prev_tp[m] = tp
                if not (0 <= b < nblk):
                    continue
                members.add(m)
                gu = fl.get("gust")
                for s in sites:
                    if s["id"] not in geoms:
                        geoms[s["id"]] = site_geometry(s, lats, lons, 10.0)
                    g = geoms[s["id"]]
                    if not g.covered:
                        continue
                    seen[s["id"]][b] = True
                    gmax = None if gu is None else float(np.nanmax(gu[g.r10])) * MS_TO_KT
                    blk = None
                    if tp is not None and (ptp is not None or fh == block_h):
                        blk = float(np.nanmax((tp - (ptp if ptp is not None else 0.0))[g.r10])) / MM_PER_IN
                    row = counts[s["id"]]
                    for j, th in enumerate(gthr):
                        row[j][b] += int(gmax is not None and gmax >= th)
                    for j, th in enumerate(rthr):
                        row[len(gthr) + j][b] += int(blk is not None and blk >= th)
    finally:
        grid_cache.clear()
        prev_tp.clear()
        prep.steps = []
    c = prep.carried
    if not members:
        return c
    k = {sid: [[(v if seen[sid][b] else None) for b, v in enumerate(row)] for row in rows]
         for sid, rows in counts.items()}
    if c is not None:
        # carried blocks keep their counts; only blocks the carry does not hold take fresh ones
        k = {sid: [[(old if old is not None else new) for old, new in zip(crow, frow)]
                   for crow, frow in zip(c.k.get(sid, []), rows)] for sid, rows in k.items()}
        if len(members) != c.members:
            log(f"ens: tail members {len(members)} vs carried {c.members}; members set to the smaller")
        log(f"ens: run {run:%Y%m%d%H} members {min(len(members), c.members)} (tail fetched, rest carried)")
        return EnsResult(run, min(len(members), c.members), k)
    log(f"ens: run {run:%Y%m%d%H} members {len(members)}")
    return EnsResult(run, len(members), k)


def run_ens(sites, fetcher, decoder, now, t0, hours, block_h=6, log=lambda m: None) -> Optional[EnsResult]:
    return execute_ens(prepare_ens(fetcher, now, t0, hours, block_h, log), sites, fetcher, decoder,
                       t0, hours, block_h, log)


# --------------------------------------------------------------------------
# Encoding, allow-list, size enforcement
# --------------------------------------------------------------------------


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def encode_masks(bits_per_hour: Sequence[int]) -> str:
    """Two lowercase hex chars per hour (8-bit mask over modelOrder)."""
    return "".join(f"{b & 0xFF:02x}" for b in bits_per_hour)


def decode_masks(s: str) -> List[int]:
    if len(s) % 2:
        raise ValueError("mask length must be even")
    return [int(s[i:i + 2], 16) for i in range(0, len(s), 2)]


def popcount(x: int) -> int:
    return bin(x).count("1")


def kn(rule_mask: str, avail_mask: str, hour: int) -> Tuple[int, int]:
    a = decode_masks(avail_mask)[hour]
    return popcount(decode_masks(rule_mask)[hour] & a), popcount(a)


def kany_digits(site: dict, rules=RULE_IDS) -> str:
    """One hex digit per hour: models in n voting yes on any rule (0 where n=0)."""
    av = decode_masks(site["avail"])
    acc = [0] * len(av)
    for r in rules:
        for i, m in enumerate(decode_masks(site["rules"][r])):
            acc[i] |= m
    return "".join(f"{popcount(a & v):x}" for a, v in zip(acc, av))


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def build_site(site: dict, order: List[str], results: Dict[str, ModelResult], hours: int,
               prev: Optional[dict], ens: Optional[EnsResult], t0: dt.datetime, prev_runs: int,
               carry: Optional[CarrySource] = None) -> dict:
    sid = site["id"]
    carried_bits = sum(1 << b for b, m in enumerate(order)
                       if results.get(m) is not None and results[m].carried and results[m].status == "ok")
    cs = None
    if carried_bits and carry is not None:
        nbm_ok = "nbm" not in order or bool(carried_bits & (1 << order.index("nbm")))
        cs = carry_site(carry, sid, carried_bits, nbm_ok, hours)
    avail, ltg = [0] * hours, [0] * hours
    rules = {r: [0] * hours for r in RULE_IDS}
    gust_by_h: List[List[float]] = [[] for _ in range(hours)]
    pw_by_h: List[List[float]] = [[] for _ in range(hours)]
    shear_by_h: List[List[float]] = [[] for _ in range(hours)]
    for bit, mid in enumerate(order):
        r = results.get(mid)
        if r is None or r.status != "ok":
            continue
        for i, sh in r.per_site.get(sid, {}).items():
            if sh.gust_kt is not None:
                gust_by_h[i].append(sh.gust_kt)
            if not r.spec.votes:
                continue
            avail[i] |= 1 << bit
            for rid in RULE_IDS:
                if sh.votes.get(rid):
                    rules[rid][i] |= 1 << bit
            if sh.ltg:
                ltg[i] |= 1 << bit
            if sh.pw_mm is not None:
                pw_by_h[i].append(sh.pw_mm)
            if sh.shear_kt is not None:
                shear_by_h[i].append(sh.shear_kt)
    rnd = lambda x: None if x is None else int(round(x))
    g_med = [rnd(_median(g)) if g else None for g in gust_by_h]
    g_lo = [rnd(min(g)) if g else None for g in gust_by_h]
    g_hi = [rnd(max(g)) if g else None for g in gust_by_h]
    pw = [rnd(_median(p) / MM_PER_IN * 100) if p else None for p in pw_by_h]
    if cs is not None:
        for i in range(hours):
            avail[i] |= cs.avail[i]
            ltg[i] |= cs.ltg[i]
            for rid in RULE_IDS:
                rules[rid][i] |= cs.rules[rid][i]
            if g_med[i] is None:          # only carried models cover this hour
                g_med[i], g_lo[i], g_hi[i] = cs.gust["med"][i], cs.gust["lo"][i], cs.gust["hi"][i]
            else:                         # min/max of the union are exact; the median stays fresh-only
                if cs.gust["lo"][i] is not None:
                    g_lo[i] = min(g_lo[i], cs.gust["lo"][i])
                if cs.gust["hi"][i] is not None:
                    g_hi[i] = max(g_hi[i], cs.gust["hi"][i])
            if pw[i] is None:
                pw[i] = cs.pw[i]
    out = {
        "id": sid, "name": site["name"],
        "pads": [dict({"id": p["id"], "lat": round(p["lat"], 4), "lon": round(p["lon"], 4)},
                      **({"note": p["note"]} if p.get("note") else {})) for p in site["pads"]],
        "tz": site["tz"],
        "avail": encode_masks(avail),
        "rules": {r: encode_masks(rules[r]) for r in RULE_IDS},
        "ltg": encode_masks(ltg),
        "gust": {"med": g_med, "lo": g_lo, "hi": g_hi},
        "pw": pw,
    }
    blk = 6
    sh = []
    for b in range(math.ceil(hours / blk)):
        vals = [_median(shear_by_h[i]) for i in range(b * blk, min(hours, b * blk + blk)) if shear_by_h[i]]
        sh.append(rnd(max(vals)) if vals else (cs.shear[b] if cs is not None else None))
    out["shear"] = {"stepH": blk, "kt": sh}
    pr = prev_block(prev, sid, t0, hours, prev_runs)
    if pr:
        out["prev"] = pr
    if ens is not None and sid in ens.k:
        exc = {"src": "ecmwf-ens", "run": iso(ens.run), "members": ens.members, "blockH": 6,
               "rowsDef": list(EXC_ROWS), "k": ens.k[sid]}
        kp = prev_exc(prev, sid, ens, t0, hours)
        if kp is not None:
            exc["kPrev3"] = kp
        out["exc"] = exc
    return out


def _shift(s: str, by: int, hours: int, width: int, fill: str) -> str:
    cells = [s[i:i + width] for i in range(0, len(s), width)]
    out = []
    for i in range(hours):
        j = i + by
        out.append(cells[j] if 0 <= j < len(cells) else fill * width)
    return "".join(out)


def prev_block(prev: Optional[dict], sid: str, t0: dt.datetime, hours: int, keep: int) -> Optional[dict]:
    """Previous run sets' kAny (one hex digit per hour) realigned to this t0. Hours the
    previous file did not cover are '0' (the v1 contract has no 'unknown' digit)."""
    if not prev or keep <= 0:
        return None
    ps = next((s for s in prev.get("sites", []) if s.get("id") == sid), None)
    if ps is None:
        return None
    by = int((t0 - parse_iso(prev["t0"])).total_seconds() // 3600)
    runs = [prev["generatedAt"]]
    kany = [_shift(kany_digits(ps, prev.get("rules", RULE_IDS)), by, hours, 1, "0")]
    old = ps.get("prev") or {}
    for r, k in zip(old.get("runs", []), old.get("kAny", [])):
        if len(runs) >= keep:
            break
        runs.append(r)
        kany.append(_shift(k, by, hours, 1, "0"))
    return {"runs": runs, "kAny": kany}


def prev_exc(prev: Optional[dict], sid: str, ens: EnsResult, t0: dt.datetime, hours: int):
    """kPrev3: the previous ENS run's member counts realigned to this file's 6-h blocks
    (interpretation: 'previous ENS run'; flagged for review)."""
    if not prev:
        return None
    ps = next((s for s in prev.get("sites", []) if s.get("id") == sid), None)
    e = (ps or {}).get("exc")
    if not e:
        return None
    src = e["k"] if e.get("run") != iso(ens.run) else e.get("kPrev3")
    if src is None:
        return None
    by = int((t0 - parse_iso(prev["t0"])).total_seconds() // 3600)
    if by % 6:
        return None
    sb, nblk = by // 6, math.ceil(hours / 6)
    return [[(row[b + sb] if 0 <= b + sb < len(row) else None) for b in range(nblk)] for row in src]


ALLOWED_KEYS = {
    "top": {"schema", "v", "generatedAt", "t0", "stepH", "hours", "modelOrder", "models", "rules",
            "sites", "attribution", "advisory", "cfgHash"},
    "model": {"id", "label", "run", "maxH", "status", "coarse", "ltg", "maxAgeH"},
    "site": {"id", "name", "pads", "tz", "avail", "rules", "ltg", "gust", "pw", "shear", "prev", "exc"},
    "pad": {"id", "lat", "lon", "note"},
    "gust": {"med", "lo", "hi"},
    "shear": {"stepH", "kt"},
    "prev": {"runs", "kAny"},
    "exc": {"src", "run", "members", "blockH", "rowsDef", "k", "kPrev3"},
}
_PII_RE = re.compile(r"(@|/Users/|/home/|\\\\|\b\d{1,3}(\.\d{1,3}){3}\b|%)")


def check_allow_list(doc: dict) -> None:
    """Raises ValueError on any key outside the allow-list or any PII-looking string."""
    def keys(o, kind, path):
        bad = set(o) - ALLOWED_KEYS[kind]
        if bad:
            raise ValueError(f"{path}: key not allowed: {sorted(bad)[0]}")
    keys(doc, "top", "$")
    for i, m in enumerate(doc["models"]):
        keys(m, "model", f"$.models[{i}]")
    for i, s in enumerate(doc["sites"]):
        keys(s, "site", f"$.sites[{i}]")
        for j, pd in enumerate(s["pads"]):
            keys(pd, "pad", f"$.sites[{i}].pads[{j}]")
        if set(s["rules"]) - set(RULE_IDS):
            raise ValueError(f"$.sites[{i}].rules: unknown rule")
        for k in ("gust", "shear", "prev", "exc"):
            if k in s:
                keys(s[k], k, f"$.sites[{i}].{k}")

    def strings(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from strings(v)
    for s in strings(doc):
        if _PII_RE.search(s):
            raise ValueError("string looks like personal data, a path, an IP or a percent")
        if re.search(r"\b(GO|NO-GO)\b", s):
            raise ValueError("commit-call wording is not allowed")


def dumps(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def enforce_budget(doc: dict, limit: int = MAX_BYTES) -> Tuple[Optional[bytes], List[str]]:
    """Degrade in design 3.3 order until <= limit. Returns (bytes or None, steps taken)."""
    steps: List[str] = []
    b = dumps(doc)
    if len(b) <= limit:
        return b, steps

    def step1():
        for s in doc["sites"]:
            if "gust" in s:
                s["gust"].pop("lo", None)
                s["gust"].pop("hi", None)
        return "drop gust.lo/hi"

    def step2():
        for s in doc["sites"]:
            if "prev" in s:
                s["prev"]["runs"] = s["prev"]["runs"][:1]
                s["prev"]["kAny"] = s["prev"]["kAny"][:1]
        return "prev to 1 run"

    def step3():
        for s in doc["sites"]:
            if "exc" in s:
                s["exc"].pop("kPrev3", None)
        return "drop exc.kPrev3"
    for fn in (step1, step2, step3):
        steps.append(fn())
        b = dumps(doc)
        if len(b) <= limit:
            return b, steps
    while len(doc["sites"]) > 1:
        dropped = doc["sites"].pop()
        steps.append(f"drop site {dropped['id']}")
        b = dumps(doc)
        if len(b) <= limit:
            return b, steps
    return None, steps


def build_doc(cfg: dict, results: Dict[str, ModelResult], ens: Optional[EnsResult], now: dt.datetime,
              t0: dt.datetime, prev: Optional[dict], carry: Optional[CarrySource] = None) -> dict:
    order = [m for m in MODEL_BIT_ORDER if m in cfg["models"]]
    hours = cfg.get("hours", HOURS)
    models = []
    for mid in order:
        r = results.get(mid)
        if r is None or r.run is None:
            continue   # missing: omitted; the client treats an absent model as missing
        models.append({"id": mid, "label": r.spec.label, "run": iso(r.run), "maxH": r.max_h,
                       "status": r.status, "coarse": r.spec.coarse, "ltg": r.spec.ltg,
                       "maxAgeH": r.max_age_h})
    attr_noaa = [MODELS[m].attribution for m in order if MODELS[m].attribution and
                 results.get(m) is not None and results[m].status == "ok"]
    attribution = []
    if attr_noaa:
        attribution.append(NOAA_ATTR.format(", ".join(attr_noaa)))
    if (results.get("ecmwf") and results["ecmwf"].status == "ok") or ens is not None:
        attribution.append("ECMWF open data, CC-BY-4.0, \u00a9 ECMWF")
    if not attribution:
        attribution.append("No model data in this run")
    return {
        "schema": SCHEMA, "v": SCHEMA_V, "generatedAt": iso(now), "t0": iso(t0), "stepH": 1,
        "hours": hours, "modelOrder": order, "models": models, "rules": list(RULE_IDS),
        "sites": [build_site(s, order, results, hours, prev, ens, t0, cfg.get("prevRuns", 3), carry)
                  for s in cfg["sites"]],
        "attribution": attribution, "advisory": ADVISORY,
        "cfgHash": config_fingerprint(cfg),
    }

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

CONFIG_KEYS = {"models", "sites", "rules", "maxAgeH", "exc", "prevRuns", "hours", "maxDownloadGB", "schedule", "minFreeGB"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")


def load_config(obj: dict) -> dict:
    """Validate a public config (sites + models). Refuses unknown keys and unknown model ids."""
    if not isinstance(obj, dict):
        raise ValueError("config must be an object")
    bad = set(obj) - CONFIG_KEYS - {"_comment"}
    if bad:
        raise ValueError(f"unknown config key: {sorted(bad)[0]}")
    models = list(obj.get("models", DEFAULT_MODELS))
    for m in models:
        if m not in MODELS:
            raise ValueError(f"unknown model id: {m}")
    if len(set(models)) != len(models) or not 1 <= len(models) <= MAX_MODELS:
        raise ValueError("models must be 1-6 unique known ids")
    sites = obj.get("sites") or []
    if not 1 <= len(sites) <= MAX_SITES:
        raise ValueError("sites must hold 1-8 entries")
    clean = []
    for s in sites:
        if not _ID.match(str(s.get("id", ""))) or not s.get("name") or not s.get("tz") or not s.get("pads"):
            raise ValueError("each site needs id, name, tz and pads")
        pads = []
        for p in s["pads"]:
            lat, lon = float(p["lat"]), float(p["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("pad lat/lon out of range")
            if not _ID.match(str(p.get("id", ""))):
                raise ValueError("pad id must be a short lowercase id")
            pad = {"id": str(p["id"]), "lat": lat, "lon": lon}
            if p.get("note"):
                pad["note"] = str(p["note"])[:60]
            pads.append(pad)
        if not 1 <= len(pads) <= 12:
            raise ValueError("each site needs 1-12 pads")
        clean.append({"id": s["id"], "name": str(s["name"]), "tz": str(s["tz"]), "pads": pads})
    rules = dict(DEFAULT_RULES)
    for k, v in (obj.get("rules") or {}).items():
        if k == "_comment":
            continue   # documentation only (labels such as "ours (proxy)")
        if k not in DEFAULT_RULES:
            raise ValueError(f"unknown rule threshold: {k}")
        rules[k] = float(v)
    ages = {m: float((obj.get("maxAgeH") or {}).get(m, MODELS[m].max_age_h)) for m in models}
    hours = int(obj.get("hours", HOURS))
    if not 1 <= hours <= HOURS:
        raise ValueError(f"hours must be 1-{HOURS}")
    min_free_gb = obj.get("minFreeGB", DEFAULT_MIN_FREE_GB)
    if isinstance(min_free_gb, bool) or not isinstance(min_free_gb, (int, float)) or not 0 <= min_free_gb <= 10000:
        raise ValueError("minFreeGB must be a number 0-10000 (below 10 needs --allow-low-min-free)")
    cap_gb = float(obj.get("maxDownloadGB", DEFAULT_MAX_DOWNLOAD_GB))
    if not 0 < cap_gb <= 64:
        raise ValueError("maxDownloadGB must be > 0 and <= 64")
    sched = {"basis": DEFAULT_SCHEDULE["basis"], "segments": [dict(x) for x in DEFAULT_SCHEDULE["segments"]]}
    for k, v in (obj.get("schedule") or {}).items():
        if k == "_comment":
            continue
        if k == "basis":
            if v not in ("fh", "board"):
                raise ValueError("schedule.basis must be \"fh\" or \"board\"")
            sched["basis"] = v
        elif k == "segments":
            segs = [{"toH": int(x["toH"]), "stepH": int(x["stepH"])} for x in v]
            if not 1 <= len(segs) <= 6:
                raise ValueError("schedule.segments: 1-6 entries")
            last = 0
            for x in segs:
                if not (last < x["toH"] <= HOURS and x["stepH"] in (1, 2, 3, 6)):
                    raise ValueError(f"schedule.segments: toH increasing to at most {HOURS}, stepH one of 1, 2, 3, 6")
                last = x["toH"]
            if last != HOURS:
                raise ValueError(f"schedule.segments: the last toH must be {HOURS}")
            sched["segments"] = segs
        else:
            raise ValueError(f"unknown schedule key: {k}")
    return {"models": models, "sites": clean, "rules": rules, "maxAgeH": ages,
            "exc": bool((obj.get("exc") or {}).get("enabled", True)),
            "prevRuns": int(obj.get("prevRuns", 3)), "hours": hours,
            "maxDownloadGB": cap_gb, "schedule": sched, "minFreeGB": float(min_free_gb)}

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def disk_free(path: str) -> int:
    """Free bytes on the volume holding `path` (tests patch this)."""
    return shutil.disk_usage(path).free


def _existing_dir(path: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    while not os.path.isdir(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    return d


def preflight_disk(out: str, min_free_bytes: int, log) -> bool:
    """Phil 2026-11-17 8:15 PM CT: refuse to start (exit 6, before any network call) when the home
    volume or the output volume has less than min_free_bytes free. Logs no local paths."""
    checks = (("home volume", os.path.expanduser("~")), ("output volume", _existing_dir(out)))
    for label, p in checks:
        try:
            free = disk_free(p)
        except OSError as e:
            log(f"disk: cannot read free space on the {label} ({type(e).__name__}); not starting "
                f"(exit {EXIT_LOW_DISK})")
            return False
        if free < min_free_bytes:
            log(f"disk: {free} bytes free on the {label}, under the {min_free_bytes} minimum "
                f"(minFreeGB); nothing downloaded or written (exit {EXIT_LOW_DISK})")
            return False
    return True


def clean_out_dir(out: str, log) -> None:
    """The output folder keeps only <name>.json and <name>.last-good.json (Phil 2026-11-17 8:15 PM
    CT). Stray <name>*.tmp files left by a crashed atomic write are removed at startup. Any other
    file is not the engine's: it is counted in the log and left alone."""
    d = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(d):
        return
    stem = os.path.splitext(os.path.basename(out))[0]
    keep = {os.path.basename(out), os.path.basename(last_good_path(out))}
    removed = other = 0
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name in keep:
            continue
        # a symlink is removed as a link (its target is never touched); directories are left alone
        if name.startswith(stem) and name.endswith(".tmp") and (os.path.islink(p) or os.path.isfile(p)):
            try:
                os.remove(p)
                removed += 1
            except OSError:
                other += 1
        else:
            other += 1
    if removed:
        log(f"out: removed {removed} stray .tmp file(s) left by an interrupted write")
    if other:
        log(f"out: {other} other item(s) in the output folder are not the engine's; left alone")


def last_good_path(out: str) -> str:
    """lwo-forecast.json -> lwo-forecast.last-good.json"""
    root, ext = os.path.splitext(out)
    return root + ".last-good" + ext


def write_atomic(path: str, data: bytes) -> None:
    """Write path.tmp, then rotate: the current file (the last good output) becomes
    <name>.last-good.json and the new file takes its place. Only these two files are kept."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)                     # a stale tmp (or a link) is removed, never written through
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if os.path.exists(path):
            os.replace(path, last_good_path(path))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_prev(path: Optional[str]) -> Optional[dict]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            d = json.load(fh)
        if d.get("schema") == SCHEMA and d.get("v") == SCHEMA_V:
            return d
    except (OSError, ValueError):
        pass
    return None


def run(cfg: dict, out: str, now: Optional[dt.datetime] = None, fetcher: Optional[Fetcher] = None,
        decoder: Optional[Decoder] = None, prev_path: Optional[str] = None, log=None,
        allow_low_min_free: bool = False) -> int:
    """One engine run. Phase 1 picks the latest cycle of each model and fetches only index files;
    the planned GRIB bytes are summed and the run aborts (exit 5) before any GRIB GET if they
    exceed maxDownloadGB. Phase 2 byte-ranges the GRIB, still capped on actual bytes. GRIB and
    index bytes live in memory only and are released per model; nothing but the output JSON and
    its .last-good copy is ever written, and an aborted run leaves both untouched."""
    t_start = time.time()
    log = log or (lambda m: print(f"[lwo-w3] {m}", file=sys.stderr))
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    t0 = now.replace(minute=0, second=0)
    # Disk guard first (Phil 2026-11-17 8:15 PM CT): nothing below runs, and no network call is
    # made, unless both volumes have minFreeGB (default 10 = 1e10 bytes) free.
    min_free_gb = float(cfg.get("minFreeGB", DEFAULT_MIN_FREE_GB))
    if min_free_gb < DEFAULT_MIN_FREE_GB and not allow_low_min_free:
        log(f"config: minFreeGB {min_free_gb:g} is below the default {DEFAULT_MIN_FREE_GB:g}; "
            f"pass --allow-low-min-free to allow it (exit {EXIT_USAGE})")
        return EXIT_USAGE
    clean_out_dir(out, log)
    if not preflight_disk(out, int(min_free_gb * GB), log):
        return EXIT_LOW_DISK
    cap = int(cfg.get("maxDownloadGB", DEFAULT_MAX_DOWNLOAD_GB) * GB)
    fetcher = fetcher or Fetcher(max_bytes=cap)
    fetcher.max_bytes = cap
    decoder = decoder or EccodesDecoder()
    hours = cfg["hours"]
    sched = cfg.get("schedule") or DEFAULT_SCHEDULE
    prev = read_prev(prev_path if prev_path is not None else out)
    carry = carry_source(prev, cfg, t0, log)
    results: Dict[str, ModelResult] = {}
    preps: Dict[str, ModelPrep] = {}
    ens_prep: Optional[EnsPrep] = None
    ens = None
    try:
        for mid in cfg["models"]:
            try:
                preps[mid] = prepare_model(MODELS[mid], cfg["sites"], fetcher, now, t0, hours,
                                           cfg["maxAgeH"][mid], sched, log, carry)
            except FetchError as e:
                log(f"model {mid}: fetch stopped ({e})")
                preps[mid] = ModelPrep(ModelResult(MODELS[mid], None, "missing", 0, cfg["maxAgeH"][mid], {}), [], [])
        ens_prep = (prepare_ens(fetcher, now, t0, hours, log=log, carry=carry, sites=cfg["sites"])
                    if cfg["exc"] else None)
        planned = fetcher.bytes + sum(p.nbytes for p in preps.values()) + (ens_prep.nbytes if ens_prep else 0)
        log(f"tls: {getattr(fetcher, 'tls_note', '') or 'not connected yet'}")
        log(f"plan: {planned} bytes incl. {fetcher.bytes} index bytes in {fetcher.requests} requests; "
            f"cap {cap}")
        if planned > cap:
            raise DownloadCapExceeded(f"planned {planned} bytes > cap {cap}; no GRIB downloaded")
        for mid, prep in preps.items():
            results[mid] = execute_model(prep, cfg["sites"], fetcher, decoder, cfg["rules"], log)
        if ens_prep is not None:
            ens = execute_ens(ens_prep, cfg["sites"], fetcher, decoder, t0, hours, log=log)
    except DownloadCapExceeded as e:
        log(f"download cap: {e}. Run aborted; nothing written, the last good output is kept "
            f"(exit {EXIT_DOWNLOAD_CAP})")
        return EXIT_DOWNLOAD_CAP
    finally:
        fetcher.close()
        for p in preps.values():
            p.plans = []
        if ens_prep is not None:
            ens_prep.steps = []
        tmp = out + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
    if not any(r.status == "ok" and r.spec.votes for r in results.values()):
        log("no voting model produced data; nothing written")
        return EXIT_NO_MODELS
    doc = build_doc(cfg, results, ens, now, t0, prev, carry)
    check_allow_list(doc)
    data, steps = enforce_budget(doc)
    for s in steps:
        log(f"size degrade: {s}")
    if data is None:
        log("over the 60 KB cap after every degrade step; nothing written")
        return EXIT_OVER_BUDGET
    write_atomic(out, data)
    log(f"wrote {len(data)} bytes; downloaded {fetcher.bytes} bytes in {fetcher.requests} requests; "
        f"{time.time() - t_start:.0f} s")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lwo_w3", description="W3 LWO forecast engine (advisory, model-only).")
    ap.add_argument("--config", required=True, help="public sites/models JSON (see sites.json)")
    ap.add_argument("--out", required=True, help="local output JSON file (written atomically)")
    ap.add_argument("--prev", default=None, help="previous output for trends (default: --out if present)")
    ap.add_argument("--now", default=None, help="override the clock, YYYY-MM-DDTHH:MM:SSZ (testing)")
    ap.add_argument("--allow-low-min-free", action="store_true",
                    help="allow a config minFreeGB below the default 10 GB")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    try:
        with open(a.config, "rb") as fh:
            cfg = load_config(json.load(fh))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"[lwo-w3] bad config: {e}", file=sys.stderr)
        return EXIT_USAGE
    if not a.out.endswith(".json"):
        print("[lwo-w3] --out must be a .json file", file=sys.stderr)
        return EXIT_USAGE
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("[lwo-w3] numpy is not installed (pip install -r requirements.txt)", file=sys.stderr)
        return EXIT_USAGE
    now = parse_iso(a.now) if a.now else None
    return run(cfg, a.out, now=now, prev_path=a.prev, allow_low_min_free=a.allow_low_min_free)


if __name__ == "__main__":
    sys.exit(main())
