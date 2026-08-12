import unittest
from io import BytesIO
from zipfile import ZipFile

import pandas as pd
import requests

from app.market_data import (
    ECB_HISTORICAL_ZIP_URL,
    FRANKFURTER_TIMESERIES_URL,
    _combine_cny_series,
    fetch_fx_usd_cny,
)
from app.strategy_engine import build_market_overview


class FakeResponse:
    def __init__(self, *, json_data=None, content=b""):
        self._json_data = json_data
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class FallbackSession:
    def get(self, url, **kwargs):
        if "query2.finance.yahoo.com" in url:
            raise requests.Timeout("Yahoo unavailable")
        if ECB_HISTORICAL_ZIP_URL in url:
            raise requests.Timeout("ECB archive unavailable")
        if FRANKFURTER_TIMESERIES_URL.split("{start}")[0] in url:
            return FakeResponse(
                json_data={
                    "rates": {
                        "2025-01-02": {"CNY": 7.25},
                        "2025-01-03": {"CNY": 7.26},
                    }
                }
            )
        raise AssertionError(f"unexpected URL: {url}")


class EcbSession:
    def get(self, url, **kwargs):
        if "query2.finance.yahoo.com" in url:
            raise requests.Timeout("Yahoo unavailable")
        if ECB_HISTORICAL_ZIP_URL in url:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                archive.writestr(
                    "eurofxref-hist.csv",
                    "Date,USD,CNY\n2025-01-02,1.10,7.70\n2025-01-03,1.11,7.77\n",
                )
            return FakeResponse(content=buffer.getvalue())
        raise AssertionError(f"unexpected URL: {url}")


class MemoryStore:
    def __init__(self):
        self.series = {}

    def read_series(self, name):
        if name not in self.series:
            raise FileNotFoundError(name)
        return self.series[name].copy()

    def write_series(self, name, series):
        self.series[name] = series.copy()

    def merge_and_write(self, name, incoming):
        existing = self.series.get(name, pd.Series(dtype=float))
        merged = incoming.copy() if existing.empty else pd.concat([existing, incoming]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        self.series[name] = merged
        return merged


class MarketDataTests(unittest.TestCase):
    def test_fx_uses_frankfurter_when_yahoo_and_ecb_fail(self):
        series, provider = fetch_fx_usd_cny(FallbackSession(), start="2025-01-01")

        self.assertEqual(provider, "Frankfurter ECB reference rates")
        self.assertEqual(series.loc[pd.Timestamp("2025-01-03")], 7.26)

    def test_fx_parses_ecb_euro_cross_rate(self):
        series, provider = fetch_fx_usd_cny(EcbSession(), start="2025-01-01")

        self.assertEqual(provider, "ECB eurofxref-hist 官方参考汇率")
        self.assertAlmostEqual(series.loc[pd.Timestamp("2025-01-03")], 7.77 / 1.11)

    def test_remote_fx_fallback_is_success_for_cny_combination(self):
        store = MemoryStore()
        dates = pd.date_range("2030-01-02", periods=2, freq="D")
        store.write_series("qqq_usd", pd.Series([100.0, 101.0], index=dates))
        store.write_series("usd_cny", pd.Series([7.0, 7.1], index=dates))
        status = _combine_cny_series(
            store,
            "nasdaq100",
            "qqq_usd",
            [
                {"name": "usd_cny", "status": "success", "source": "ECB eurofxref-hist"},
                {"name": "qqq_usd", "status": "success", "source": "Yahoo Finance QQQ"},
            ],
        )

        self.assertEqual(status["status"], "success")
        self.assertIn("ECB eurofxref-hist", status["message"])
        self.assertAlmostEqual(store.read_series("nasdaq100").loc[dates[-1]], 101.0 * 7.1)

    def test_market_overview_excludes_manual_cash(self):
        store = MemoryStore()
        dates = pd.date_range("2030-01-02", periods=2, freq="D")
        for asset in ("dividend_low_vol", "nasdaq100", "gold", "long_bond"):
            store.write_series(asset, pd.Series([100.0, 101.0], index=dates))

        overview = build_market_overview(store, dates[-1])

        self.assertEqual(len(overview), 4)
        self.assertNotIn("cash", {item["asset"] for item in overview})


if __name__ == "__main__":
    unittest.main()
