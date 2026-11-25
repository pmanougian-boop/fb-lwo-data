# Engine source

`lwo_w3.py` is the W3 LWO forecast engine, copied verbatim so this public
repository can refresh `lwo-forecast.json` without checking out the private
Flight Board repository (the Actions token cannot read that repo, and this
workflow adds no secrets).

| | |
|---|---|
| Repository | `pmanougian-boop/flight-board` |
| Commit | `97c33dcb4c4c4729a8b725352c173fab85c542e4` (main, 2026-11-19) |
| Path | `tools/lwo-engine/lwo_w3.py` |
| Git blob | `c394694340b970bd41c57abe7cfe0b50c4a87deb` |
| Version | `0.1.0` (`__version__` in the script) |
| Lock | `tools/lwo-engine/requirements.lock` (same commit; CPython 3.13, linux x86_64 and macOS arm64) |
| Sites | `tools/lwo-engine/sites.json` from that commit, plus `minFreeGB: 1` |

`6c4a7f6f` is not a commit on `flight-board`. The file above is the engine on
main at the commit in the table.

`sites.json` keeps the shipped model list: HRRR, RRFS, GFS, ECMWF, with ECMWF
ENS exceedance counts turned on. NBM and GEFS are implemented in the engine and
left off, which is how the file Flight Board reads is produced today. Turning
them on would change `modelOrder` and every mask.

The engine performs HTTPS GET requests only, to NOAA Open Data buckets
(`noaa-hrrr-bdp-pds`, `noaa-rrfs-ops-pds`, `noaa-gfs-bdp-pds`, and, if enabled,
`noaa-nbm-grib2-pds` and `noaa-gefs-pds` on `s3.amazonaws.com`) and to
`data.ecmwf.int`. It does not use an API key. GRIB stays in memory. The only
files it writes are the JSON passed to `--out` and that file's `.last-good`
sibling.
