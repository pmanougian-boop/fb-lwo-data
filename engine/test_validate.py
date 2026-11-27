"""Offline checks for the publish gate.

The fixture is engine/fixtures/forecast-valid.json. Tests never open the
committed lwo-forecast.json, and every freshness check gets an injected clock.
The same document is fresh or stale only because of that clock, so a real
publish cannot make this suite fail.
"""
import copy
import datetime as dt
import json
import pathlib
import unittest

import validate_forecast as v

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "forecast-valid.json"
NOW = dt.datetime(2026, 6, 1, 18, 5, tzinfo=dt.timezone.utc)


def _iso(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def at_clock(doc: dict, now: dt.datetime) -> dict:
    """Restamp a copy so it was produced at `now` (t0 on the hour, runs 6 h earlier)."""
    out = copy.deepcopy(doc)
    t0 = now.replace(minute=0, second=0, microsecond=0)
    run = _iso(t0 - dt.timedelta(hours=6))
    out["generatedAt"] = _iso(now.replace(microsecond=0))
    out["t0"] = _iso(t0)
    for model in out["models"]:
        model["run"] = run
    for site in out["sites"]:
        site["exc"]["run"] = run
    return out


class ValidateFixture(unittest.TestCase):
    def test_fixture_shape_passes_without_a_clock(self):
        self.assertEqual(v.validate(load_fixture(), require_fresh=False), [])

    def test_fresh_against_the_injected_clock(self):
        doc = at_clock(load_fixture(), NOW)
        self.assertEqual(v.validate(doc, now=NOW, require_fresh=True), [])

    def test_same_document_is_stale_at_a_later_injected_clock(self):
        doc = at_clock(load_fixture(), NOW)
        later = NOW + dt.timedelta(hours=24)
        errs = v.validate(doc, now=later, require_fresh=True)
        self.assertTrue(any("generatedAt" in e for e in errs))

    def test_stored_fixture_is_stale_at_the_injected_clock(self):
        errs = v.validate(load_fixture(), now=NOW, require_fresh=True)
        self.assertTrue(any("generatedAt" in e for e in errs))

    def test_missing_ens_is_rejected(self):
        doc = load_fixture()
        for site in doc["sites"]:
            site.pop("exc", None)
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("ENS" in e for e in errs))

    def test_short_mask_is_rejected(self):
        doc = load_fixture()
        doc["sites"][0]["avail"] = "0f" * 47
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("avail" in e for e in errs))

    def test_absurd_gust_is_rejected(self):
        doc = load_fixture()
        doc["sites"][0]["gust"]["med"][0] = 999
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("gust" in e for e in errs))

    def test_missing_voting_model_is_rejected(self):
        doc = load_fixture()
        doc["models"] = [m for m in doc["models"] if m["id"] != "ecmwf"]
        doc["modelOrder"] = ["hrrr", "rrfs", "gfs"]
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("ecmwf" in e or "modelOrder" in e for e in errs))

    def test_log_byte_line(self):
        info = v.parse_log("noise\nwrote 9123 bytes; downloaded 2503123456 bytes in 2956 requests; 750 s\n")
        self.assertEqual(info["bytesDownloaded"], 2503123456)
        self.assertEqual(info["requests"], 2956)
        self.assertEqual(info["durationSecLogged"], 750)


if __name__ == "__main__":
    unittest.main()
