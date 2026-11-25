# fb-lwo-data

Public, machine-generated launch-weather model summaries for Flight Board (4 sites: LC-39A, Starbase TX, Starbase LA (planned, assumed location), SLC-4E).

- `lwo-forecast.json`: latest run only, 0 to 48 h, from NOAA HRRR, RRFS, GFS and ECMWF open data (CC BY 4.0, (c) ECMWF).
- `lwo-status.json`: the run that produced the file above (cycle, duration, bytes, models). It appears after the first scheduled publish.
- Rules are proxies of NASA-STD-4010B launch-weather criteria. This is decision support, not an official forecast.

## Refresh

`.github/workflows/lwo-forecast.yml` runs the engine in this repository. The engine is the W3 script from Flight Board, copied in `engine/` (source commit and blob are in `engine/SOURCE.md`). It downloads GRIB into memory from public NOAA S3 buckets and `data.ecmwf.int`, then writes one JSON file. No API key and no repository secret.

The workflow runs at 05:30, 11:30, 17:30 and 23:30 UTC. That is about five and a half hours after the 00/06/12/18Z cycles, when the HRRR and RRFS runs that reach f048, and GFS, are usually published. ECMWF open data is often later; the engine keeps the newest cycle that is actually on the server, looking back at most 24 hours. GitHub may start a scheduled run later than the cron time. The engine tolerates that.

A run publishes only when every voting model (HRRR, RRFS, GFS, ECMWF) is present with status `ok`, hours 0 through 47 are present, values sit in a physical range, and ECMWF ENS is present on all four sites. Otherwise the job fails and the previous `lwo-forecast.json` stays. NBM and GEFS stay off, matching the file Flight Board reads today.

GitHub runs a `schedule` only from the default branch. The cron in this workflow does nothing until the file is on `main`. A pull request runs the engine and uploads `staging/lwo-forecast.json` as an artifact. It does not commit, and it does not change the Pages file.

### Turn the schedule on

Merge the pull request into `main`. The next 05:30, 11:30, 17:30 or 23:30 UTC run publishes. To publish once without waiting, open Actions, choose **LWO forecast**, run the workflow on `main`.

### Turn the schedule off

Actions → **LWO forecast** → the `...` menu → **Disable workflow**. The last good `lwo-forecast.json` stays on Pages. Enable the workflow again to resume. To undo one published file, revert that commit; the history of `lwo-forecast.json` is the rollback.

### Runner

`ubuntu-latest` on a public repository: 4 vCPU, 16 GB RAM, 14 GB SSD. The measured engine is about 1.5 GB of RAM, about 2.5 GB of GRIB that is not written to disk, and about 12 to 16 minutes. The job limit is 50 minutes. The engine's own download cap is 4 GB. `minFreeGB` is 1 in `engine/sites.json` because a 14 GB runner image does not leave the engine's 10 GB default free. That setting is not part of `cfgHash`.

Standard GitHub-hosted runners in a public repository are free and do not draw from included compute. Larger runners are not used; those are billed even on public repositories. See [GitHub Actions billing](https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions) and [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
