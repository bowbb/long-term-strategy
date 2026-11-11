from __future__ import annotations

import unittest
from uuid import uuid4
from pathlib import Path

import pandas as pd

from app.housing import housing_schedule, housing_transfer_plan, validate_housing
from app.market_data import MarketStore
from app.strategy_engine import ALL_ASSETS, MARKET_ASSETS, calculate_plan


class PlanStore:
    def __init__(self, housing=None, series=None):
        index = pd.bdate_range("2024-01-01", "2026-11-10")
        self.series = series or {asset: pd.Series(100.0, index=index) for asset in MARKET_ASSETS}
        self.settings = {"housing": housing or {}}

    def load_settings(self):
        return self.settings

    def read_series(self, asset):
        return self.series[asset].copy()


class HousingTests(unittest.TestCase):
    config = {"enabled": True, "purchase_month": "2027-11", "target_amount": 120000}

    def test_schedule_starts_one_year_before_and_finishes_in_purchase_month(self):
        before = housing_schedule(self.config, "2026-10-31")
        start = housing_schedule(self.config, "2026-11-01")
        next_month = housing_schedule(self.config, "2026-12-01")
        final = housing_schedule(self.config, "2027-11-01")
        self.assertEqual(before["required_amount"], 0)
        self.assertEqual(start["required_amount"], 60000)
        self.assertEqual(next_month["required_amount"], 65000)
        self.assertEqual(final["required_amount"], 120000)
        self.assertEqual(len(final["schedule"]), 13)
        self.assertEqual(final["schedule"][-1]["month"], "2027-11")
        self.assertEqual(sum(row["amount"] for row in final["schedule"]), 120000)

    def test_uneven_cents_and_year_boundary(self):
        result = housing_schedule(
            {**self.config, "purchase_month": "2028-01", "target_amount": 100.01}, "2028-01-31"
        )
        self.assertEqual(result["start_month"], "2027-01")
        self.assertEqual(result["required_amount"], 100.01)
        self.assertEqual(round(sum(row["amount"] for row in result["schedule"]), 2), 100.01)
        installments = [round(row["amount"] * 100) for row in result["schedule"][1:]]
        self.assertLessEqual(max(installments) - min(installments), 1)

    def test_housing_progress_is_capped_at_schedule_with_excess_bonds_for_investment(self):
        missed = housing_transfer_plan(self.config, "2027-01-01", 200000, 50000)
        completed = housing_transfer_plan(self.config, "2027-01-01", 200000, 70000)
        excess = housing_transfer_plan(self.config, "2027-01-01", 200000, 75000)
        self.assertEqual(missed["transfer_amount"], 20000)
        self.assertEqual(completed["transfer_amount"], 0)
        self.assertEqual(excess["target_reserved"], 70000)
        self.assertEqual(excess["current_reserved"], 70000)
        self.assertEqual(excess["investment_total"], 130000)

    def test_insufficient_assets_are_capped_and_shortfall_reported(self):
        plan = housing_transfer_plan(self.config, "2026-11-01", 30000, 10000)
        self.assertEqual(plan["transfer_amount"], 20000)
        self.assertEqual(plan["shortfall"], 30000)
        self.assertEqual(plan["investment_total"], 0)

    def test_disabled_plan_returns_all_bonds_to_ordinary_portfolio(self):
        plan = housing_transfer_plan({"enabled": False}, "2026-11-01", 200000, 60000)
        self.assertEqual(plan["transfer_amount"], 0)
        self.assertEqual(plan["investment_total"], 200000)
        self.assertEqual(plan["target_reserved"], 0)
        self.assertEqual(plan["current_reserved"], 0)

    def test_invalid_settings(self):
        for change in (
            {"target_amount": "nan"}, {"target_amount": "inf"}, {"target_amount": -1},
            {"target_amount": 0}, {"target_amount": 0.001},
            {"purchase_month": "2027-13"}, {"purchase_month": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_housing({**self.config, **change})

    def test_nasdaq_stays_at_fifty_percent_even_without_history(self):
        store = PlanStore()
        store.series["nasdaq100"] = pd.Series(dtype=float)
        store.series["dividend_low_vol"].iloc[-1] = 96
        store.series["gold"].iloc[-1] = 96
        plan = calculate_plan(store, {"cash": 100000}, "2026-11-11", use_crossing_history=True)
        self.assertEqual(plan["rows"][0]["asset"], "nasdaq100")
        self.assertEqual(plan["target_weights"]["nasdaq100"], 0.5)
        self.assertAlmostEqual(plan["target_weights"]["dividend_low_vol"], 0.15)
        self.assertAlmostEqual(plan["target_weights"]["gold"], 0.05)
        self.assertAlmostEqual(plan["target_weights"]["long_bond"], 0.15)
        self.assertAlmostEqual(plan["target_weights"]["cash"], 0.15)
        self.assertEqual(plan["rows"][0]["market"]["action"], "monthly_rebalance")
        self.assertEqual(plan["strategy"]["rebalance_frequency"], "monthly")

    def test_nasdaq_below_lower_band_still_rebalances_to_base_weight(self):
        store = PlanStore()
        store.series["nasdaq100"].iloc[-1] = 50
        plan = calculate_plan(store, {"nasdaq100": 80000, "cash": 20000},
                              "2026-11-11", use_crossing_history=True)
        self.assertEqual(plan["rows"][0]["target_value"], 50000)
        self.assertEqual(plan["rows"][0]["trade_value"], -30000)
        self.assertIsNone(plan["rows"][0]["market"]["ma250"])

    def test_housing_is_excluded_and_combined_trades_conserve_total(self):
        store = PlanStore(self.config)
        holdings = {"nasdaq100": 100000, "long_bond": 50000, "cash": 50000}
        plan = calculate_plan(store, holdings, "2026-11-11", use_crossing_history=True)
        rows = {row["asset"]: row for row in plan["rows"]}
        self.assertEqual(plan["current_total"], 200000)
        self.assertEqual(plan["investment_total"], 140000)
        self.assertAlmostEqual(rows["nasdaq100"]["target_value"], 70000)
        self.assertAlmostEqual(rows["nasdaq100"]["trade_value"], -30000)
        self.assertEqual(rows["housing"]["trade_value"], 10000)
        self.assertEqual(rows["housing"]["target_value"], 60000)
        self.assertEqual(plan["rows"][-1]["asset"], "housing")
        self.assertAlmostEqual(rows["long_bond"]["target_value"], 7000)
        self.assertEqual(rows["long_bond"]["current_value"], 0)
        self.assertEqual(rows["long_bond"]["target_weight"], 0.05)
        self.assertAlmostEqual(plan["housing"]["bond_total_target"], 67000)
        self.assertAlmostEqual(plan["housing"]["bond_net_trade"], 17000)
        self.assertEqual(rows["long_bond"]["signal_text"], "基础仓位 + 红利/黄金转入")
        self.assertEqual(rows["cash"]["signal_text"], "基础仓位 + 红利/黄金转入")
        self.assertAlmostEqual(sum(row["target_value"] for row in plan["rows"]), 200000)
        self.assertAlmostEqual(sum(row["trade_value"] for row in plan["rows"]), 0)
        self.assertAlmostEqual(sum(plan["target_weights"].values()), 1)

        # Updating actual holdings after executing the plan makes a second calculation idempotent.
        executed = {asset: rows[asset]["target_value"] for asset in ALL_ASSETS}
        executed["long_bond"] += rows["housing"]["target_value"]
        again = calculate_plan(store, executed, "2026-11-11", use_crossing_history=True)
        self.assertEqual(again["housing"]["transfer_amount"], 0)
        self.assertTrue(all(abs(row["trade_value"]) < 0.01 for row in again["rows"]))

    def test_current_bonds_are_split_between_housing_and_ordinary_strategy(self):
        plan = calculate_plan(PlanStore(self.config), {"long_bond": 80000, "cash": 120000},
                              "2026-11-11", use_crossing_history=True)
        bond = next(row for row in plan["rows"] if row["asset"] == "long_bond")
        self.assertEqual(plan["housing"]["current_reserved"], 60000)
        self.assertEqual(bond["current_value"], 20000)
        self.assertEqual(bond["target_value"], 7000)
        self.assertEqual(bond["trade_value"], -13000)
        self.assertEqual(plan["housing"]["bond_total_target"], 67000)
        self.assertEqual(plan["housing"]["bond_net_trade"], -13000)
        self.assertEqual(sum(row["current_value"] for row in plan["rows"]), 200000)

    def test_disabled_plan_ignores_legacy_reserve_and_uses_normal_weights(self):
        plan = calculate_plan(PlanStore(), {"long_bond": 80000, "cash": 120000,
                                           "housing_reserved": 80000}, "2026-11-11")
        bond = next(row for row in plan["rows"] if row["asset"] == "long_bond")
        self.assertEqual(plan["investment_total"], 200000)
        self.assertEqual(bond["target_value"], 10000)
        self.assertEqual(bond["signal_text"], "基础仓位 + 红利/黄金转入")

    def test_strategy_bond_allocations_remain_separate_before_housing_schedule(self):
        store = PlanStore({**self.config, "purchase_month": "2028-11"})
        store.series["dividend_low_vol"].iloc[-1] = 96
        store.series["gold"].iloc[-1] = 96
        plan = calculate_plan(store, {"cash": 100000}, "2026-11-11", use_crossing_history=True)
        bond = next(row for row in plan["rows"] if row["asset"] == "long_bond")
        self.assertAlmostEqual(bond["target_value"], 15000)
        self.assertEqual(plan["housing"]["target_reserved"], 0)
        executed = {row["asset"]: row["target_value"] for row in plan["rows"] if row["asset"] in ALL_ASSETS}
        again = calculate_plan(store, executed, "2026-11-11", use_crossing_history=True)
        self.assertTrue(all(abs(row["trade_value"]) < 0.01 for row in again["rows"]))

    def test_housing_and_defensive_releases_are_added_without_changing_fifty_percent_nasdaq(self):
        store = PlanStore(self.config)
        store.series["dividend_low_vol"].iloc[-1] = 96
        store.series["gold"].iloc[-1] = 96
        plan = calculate_plan(store, {"cash": 200000}, "2026-11-11", use_crossing_history=True)
        rows = {row["asset"]: row for row in plan["rows"]}
        self.assertEqual(rows["nasdaq100"]["target_weight"], 0.5)
        self.assertAlmostEqual(rows["nasdaq100"]["target_value"], 70000)
        self.assertAlmostEqual(rows["long_bond"]["target_weight"], 0.15)
        self.assertAlmostEqual(rows["long_bond"]["target_value"], 21000)
        self.assertEqual(rows["housing"]["target_value"], 60000)
        self.assertAlmostEqual(plan["housing"]["bond_total_target"], 81000)
        self.assertAlmostEqual(sum(row["target_value"] for row in plan["rows"]), 200000)
        self.assertAlmostEqual(sum(row["trade_value"] for row in plan["rows"]), 0)
        self.assertAlmostEqual(rows["long_bond"]["commission"], 81000 * 0.00006)
        self.assertEqual(rows["housing"]["commission"], 0)
        self.assertAlmostEqual(plan["estimated_commission"], sum(row["commission"] for row in plan["rows"]))

    def test_housing_budget_larger_than_assets_has_no_negative_or_duplicate_amounts(self):
        plan = calculate_plan(PlanStore(self.config), {"cash": 30000}, "2026-11-11")
        self.assertEqual(plan["investment_total"], 0)
        self.assertEqual(plan["housing"]["shortfall"], 30000)
        self.assertEqual(plan["housing"]["bond_total_target"], 30000)
        self.assertTrue(all(row["current_value"] >= 0 and row["target_value"] >= 0 for row in plan["rows"]))
        self.assertEqual(sum(row["current_value"] for row in plan["rows"]), 30000)
        self.assertEqual(sum(row["target_value"] for row in plan["rows"]), 30000)
        self.assertEqual(sum(row["trade_value"] for row in plan["rows"]), 0)

    def test_persisted_housing_survives_store_reload_and_migration(self):
        folder = Path(__file__).resolve().parent / f".housing-test-{uuid4().hex}"
        folder.mkdir()
        try:
            store = MarketStore(folder)
            store.ensure_store()
            store.save_settings({"refresh_time": "06:30", "timezone": "Asia/Shanghai", "housing": self.config})
            store.save_portfolio_input({"long_bond": 70000, "housing_reserved": 60000})
            store.save_calculation({"rows": [{"asset": "housing"}]})
            reloaded = MarketStore(folder)
            self.assertEqual(reloaded.load_settings()["housing"], self.config)
            self.assertNotIn("housing_reserved", reloaded.load_portfolio_input())
            self.assertEqual(reloaded.load_portfolio_input()["long_bond"], 70000)
            self.assertIsNotNone(reloaded.load_calculation())
        finally:
            for name in ("settings.json", "source_schema.json", "refresh_log.json", "portfolio_input.json",
                         "last_calculation.json"):
                (folder / name).unlink(missing_ok=True)
            (folder / "prices").rmdir()
            folder.rmdir()


if __name__ == "__main__":
    unittest.main()
