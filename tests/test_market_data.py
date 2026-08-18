import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import requests

from app.market_data import (
    CSINDEX_HISTORY_URL,
    ECB_HISTORICAL_ZIP_URL,
    FRED_CSV_URL,
    SSE_DAYK_URL,
    MarketStore,
    _combine_cny_series,
    fetch_csindex_history,
    fetch_fx_usd_cny,
    fetch_sse_daily,
)
from app.strategy_engine import build_market_overview


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", text=""):
        self._json_data = json_data
        self.content = content
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class FredSession:
    def get(self, url, **kwargs):
        if FRED_CSV_URL in url:
            return FakeResponse(text="observation_date,DEXCHUS\n2025-01-02,7.25\n2025-01-03,7.26\n")
        raise AssertionError(f"unexpected URL: {url}")


class EcbSession:
    def get(self, url, **kwargs):
        if FRED_CSV_URL in url:
            raise requests.Timeout("FRED unavailable")
        if ECB_HISTORICAL_ZIP_URL in url:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                archive.writestr(
                    "eurofxref-hist.csv",
                    "Date,USD,CNY\n2025-01-02,1.10,7.70\n2025-01-03,1.11,7.77\n",
                )
            return FakeResponse(content=buffer.getvalue())
        raise AssertionError(f"unexpected URL: {url}")


class OfficialSseSession:
    def get(self, url, **kwargs):
        if SSE_DAYK_URL.format(code="512890") in url:
            return FakeResponse(
                json_data={
                    "kline": [
                        [20260813, 1.15, 1.16, 1.14, 1.157, 1, 1],
                        [20260814, 1.157, 1.161, 1.151, 1.155, 1, 1],
                    ]
                }
            )
        raise AssertionError(f"unexpected URL: {url}")


class OfficialCsindexSession:
    def get(self, url, **kwargs):
        if url == CSINDEX_HISTORY_URL:
            return FakeResponse(
                json_data={
                    "data": [
                        {"tradeDate": "20260813", "close": "1234.56"},
                        {"tradeDate": "20260814", "close": "1238.91"},
                    ]
                }
            )
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

    def replace_series(self, name, series):
        self.write_series(name, series)
        return series.copy()


class MarketDataTests(unittest.TestCase):
    def test_fx_prefers_fred_official_series(self):
        series, provider = fetch_fx_usd_cny(FredSession(), start="2025-01-01")

        self.assertEqual(provider, "FRED DEXCHUS")
        self.assertEqual(series.loc[pd.Timestamp("2025-01-03")], 7.26)

    def test_fx_uses_ecb_when_fred_fails(self):
        series, provider = fetch_fx_usd_cny(EcbSession(), start="2025-01-01")

        self.assertEqual(provider, "ECB eurofxref-hist 官方参考汇率")
        self.assertAlmostEqual(series.loc[pd.Timestamp("2025-01-03")], 7.77 / 1.11)

    def test_sse_parser_returns_raw_official_close(self):
        series, provider = fetch_sse_daily(OfficialSseSession(), "512890", start="2026-01-01")

        self.assertEqual(provider, "SSE 官方日线 512890")
        self.assertAlmostEqual(series.loc[pd.Timestamp("2026-08-14")], 1.155)

    def test_csindex_parser_returns_total_return_close(self):
        series, provider = fetch_csindex_history(
            OfficialCsindexSession(), "H20269", start="2026-01-01"
        )

        self.assertEqual(provider, "中证指数官方 H20269 全收益指数")
        self.assertAlmostEqual(series.loc[pd.Timestamp("2026-08-14")], 1238.91)

    def test_official_cny_combination_replaces_history(self):
        store = MemoryStore()
        dates = pd.date_range("2030-01-02", periods=2, freq="D")
        store.write_series("qqq_usd", pd.Series([100.0, 101.0], index=dates))
        fx_dates = dates.append(pd.DatetimeIndex([pd.Timestamp("2030-01-04")]))
        store.write_series(
            "usd_cny",
            pd.Series([7.0, 7.1, 7.2], index=fx_dates),
        )
        status = _combine_cny_series(
            store,
            "nasdaq100",
            "qqq_usd",
            [
                {"name": "usd_cny", "status": "success", "source": "FRED DEXCHUS"},
                {"name": "qqq_usd", "status": "success", "source": "Nasdaq 官方 QQQ 日线"},
            ],
        )

        self.assertEqual(status["status"], "success")
        self.assertAlmostEqual(store.read_series("nasdaq100").loc[dates[-1]], 101.0 * 7.1)
        self.assertEqual(list(store.read_series("nasdaq100").index), list(dates))

    def test_derived_cny_series_keeps_previous_cache_when_dependency_fails(self):
        store = MemoryStore()
        dates = pd.date_range("2030-01-02", periods=2, freq="D")
        store.write_series("qqq_usd", pd.Series([100.0, 101.0], index=dates))
        store.write_series("usd_cny", pd.Series([7.0, 7.1], index=dates))
        previous = pd.Series([600.0, 601.0], index=dates)
        store.write_series("nasdaq100", previous)

        status = _combine_cny_series(
            store,
            "nasdaq100",
            "qqq_usd",
            [
                {"name": "usd_cny", "status": "warning", "source": "local_cache"},
                {"name": "qqq_usd", "status": "success", "source": "Nasdaq 官方 QQQ 日线"},
            ],
        )

        self.assertEqual(status["status"], "warning")
        self.assertTrue(status["fallback"])
        pd.testing.assert_series_equal(store.read_series("nasdaq100"), previous)

    def test_market_store_rejects_implicit_scale_change(self):
        store = MarketStore(Path("."))
        dates = pd.date_range("2026-08-13", periods=2, freq="D")
        existing = pd.Series([1000.0, 1010.0], index=dates)
        store.read_series = lambda name: existing.copy()
        store.write_series = lambda name, series: None
        with self.assertRaises(RuntimeError):
            store.merge_and_write("demo", pd.Series([1.0, 1.01], index=dates))

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
