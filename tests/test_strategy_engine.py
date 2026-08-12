from __future__ import annotations

import unittest

import pandas as pd

from app.strategy_engine import (
    MARKET_ASSETS,
    _commission,
    _signal_context,
    _trend_snapshot,
    calculate_plan,
)


class MemoryStore:
    def __init__(self, series: dict[str, pd.Series]):
        self.series = series

    def read_series(self, name: str) -> pd.Series:
        return self.series[name].copy()


class StrategyEngineTests(unittest.TestCase):
    def test_commission_is_zero_when_no_order_is_needed(self):
        self.assertEqual(_commission(0.01, 0.00006, 0.3), 0.0)
        self.assertEqual(_commission(0.009, 0.00006, 0.3), 0.0)
        self.assertEqual(_commission(100.0, 0.00006, 0.3), 0.3)

    def test_hysteresis_holds_previous_state_inside_band(self):
        index = pd.bdate_range("2024-01-01", periods=250)
        inside_band = pd.Series([100.0] * 249 + [102.0], index=index)

        held_on = _trend_snapshot(inside_band, 250, 0.03, 1.0)
        held_off = _trend_snapshot(inside_band, 250, 0.03, 0.0)

        self.assertTrue(held_on["available"])
        self.assertEqual(held_on["action"], "hold_previous")
        self.assertEqual(held_on["multiplier"], 1.0)
        self.assertEqual(held_off["multiplier"], 0.0)

    def test_hysteresis_switches_only_outside_three_percent_band(self):
        index = pd.bdate_range("2024-01-01", periods=250)
        above = pd.Series([100.0] * 249 + [104.0], index=index)
        below = pd.Series([100.0] * 249 + [96.0], index=index)

        self.assertEqual(_trend_snapshot(above, 250, 0.03, 0.0)["multiplier"], 1.0)
        self.assertEqual(_trend_snapshot(below, 250, 0.03, 1.0)["multiplier"], 0.0)

    def test_signal_uses_previous_complete_month_and_next_china_trading_day(self):
        calendar = pd.bdate_range("2020-01-01", "2021-03-05")
        store = MemoryStore({"csi300": pd.Series(100.0, index=calendar)})

        signal_date, execution_date = _signal_context(store, pd.Timestamp("2021-03-05"))

        self.assertEqual(signal_date, pd.Timestamp("2021-02-26"))
        self.assertEqual(execution_date, pd.Timestamp("2021-03-01"))

    def test_missing_history_moves_entire_base_weight_to_long_bond(self):
        calendar = pd.bdate_range("2019-01-01", "2021-03-05")
        rising = pd.Series(range(1000, 1000 + len(calendar)), index=calendar, dtype=float)
        series = {asset: rising for asset in MARKET_ASSETS}
        series["gold"] = rising.iloc[-100:]
        store = MemoryStore(series)

        holdings = {
            "csi300": 0.0,
            "dividend_low_vol": 0.0,
            "star50": 0.0,
            "nasdaq100": 0.0,
            "gold": 0.0,
            "long_bond": 0.0,
            "cash": 1000.0,
        }
        plan = calculate_plan(store, holdings, pd.Timestamp("2021-03-05"))

        self.assertAlmostEqual(plan["target_weights"]["dividend_low_vol"], 0.30)
        self.assertAlmostEqual(plan["target_weights"]["nasdaq100"], 0.50)
        self.assertAlmostEqual(plan["target_weights"]["gold"], 0.0)
        self.assertAlmostEqual(plan["target_weights"]["long_bond"], 0.15)
        self.assertAlmostEqual(plan["target_weights"]["cash"], 0.05)
        self.assertAlmostEqual(plan["estimated_commission"], 0.90)


if __name__ == "__main__":
    unittest.main()
