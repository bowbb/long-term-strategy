from __future__ import annotations

import unittest

import pandas as pd

from app.strategy_engine import (
    MARKET_ASSETS,
    _commission,
    _signal_context,
    _signal_text,
    _trend_snapshot,
    calculate_plan,
)


class MemoryStore:
    def __init__(self, series: dict[str, pd.Series]):
        self.series = series

    def read_series(self, name: str) -> pd.Series:
        return self.series[name].copy()


class StrategyEngineTests(unittest.TestCase):
    def test_signal_text_distinguishes_strategy_state_from_holdings(self):
        self.assertEqual(
            _signal_text({"action": "hold_previous", "multiplier": 1.0}),
            "滞回区间内，暂时不动",
        )
        self.assertEqual(
            _signal_text({"action": "hold_previous", "multiplier": 0.0}),
            "滞回区间内，暂时不动",
        )

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

    def test_signal_uses_latest_completed_day_and_calculation_day_for_execution(self):
        calendar = pd.bdate_range("2020-01-01", "2021-03-04")
        store = MemoryStore({"dividend_low_vol": pd.Series(100.0, index=calendar)})

        signal_date, execution_date = _signal_context(store, pd.Timestamp("2021-03-05"))

        self.assertEqual(signal_date, pd.Timestamp("2021-03-04"))
        self.assertEqual(execution_date, pd.Timestamp("2021-03-05"))

    def test_inside_band_uses_current_holding_as_previous_state(self):
        calendar = pd.bdate_range("2019-01-01", "2021-03-04")
        flat = pd.Series(100.0, index=calendar)
        store = MemoryStore({asset: flat for asset in MARKET_ASSETS})

        invested = {
            "dividend_low_vol": 300.0,
            "nasdaq100": 0.0,
            "gold": 0.0,
            "long_bond": 0.0,
            "cash": 700.0,
        }
        uninvested = dict(invested, dividend_low_vol=0.0, cash=1000.0)

        invested_plan = calculate_plan(store, invested, pd.Timestamp("2021-03-05"))
        uninvested_plan = calculate_plan(store, uninvested, pd.Timestamp("2021-03-05"))

        self.assertAlmostEqual(invested_plan["target_weights"]["dividend_low_vol"], 0.30)
        self.assertAlmostEqual(uninvested_plan["target_weights"]["dividend_low_vol"], 0.0)
        invested_row = next(row for row in invested_plan["rows"] if row["asset"] == "dividend_low_vol")
        uninvested_row = next(row for row in uninvested_plan["rows"] if row["asset"] == "dividend_low_vol")
        self.assertEqual(invested_row["signal_text"], "滞回区间内，暂时不动")
        self.assertEqual(uninvested_row["signal_text"], "滞回区间内，暂时不动")

    def test_missing_history_moves_entire_base_weight_to_long_bond(self):
        calendar = pd.bdate_range("2019-01-01", "2021-03-05")
        rising = pd.Series(range(1000, 1000 + len(calendar)), index=calendar, dtype=float)
        series = {asset: rising for asset in MARKET_ASSETS}
        series["gold"] = rising.iloc[-100:]
        store = MemoryStore(series)

        holdings = {
            "dividend_low_vol": 0.0,
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
