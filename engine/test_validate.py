"""Offline checks for the publish gate. No network."""
import datetime as dt
import json
import pathlib
import unittest

import validate_forecast as v

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE = ROOT / "lwo-forecast.json"


class ValidateLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(LIVE.read_text())

    def test_live_file_matches_the_publish_shape(self):
        self.assertEqual(v.validate(self.doc, require_fresh=False), [])

    def test_live_file_is_not_fresh(self):
        now = dt.datetime(2026, 11, 25, 18, 0, tzinfo=dt.timezone.utc)
        errs = v.validate(self.doc, now=now, require_fresh=True)
        self.assertTrue(any("generatedAt" in e for e in errs))

    def test_missing_ens_is_rejected(self):
        doc = json.loads(json.dumps(self.doc))
        for s in doc["sites"]:
            s.pop("exc", None)
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("ENS" in e for e in errs))

    def test_short_mask_is_rejected(self):
        doc = json.loads(json.dumps(self.doc))
        doc["sites"][0]["avail"] = "0f" * 47
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("avail" in e for e in errs))

    def test_absurd_gust_is_rejected(self):
        doc = json.loads(json.dumps(self.doc))
        doc["sites"][0]["gust"]["med"][0] = 999
        errs = v.validate(doc, require_fresh=False)
        self.assertTrue(any("gust" in e for e in errs))

    def test_missing_voting_model_is_rejected(self):
        doc = json.loads(json.dumps(self.doc))
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
