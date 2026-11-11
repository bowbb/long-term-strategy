from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from app import main


class SettingsRoutesTests(unittest.TestCase):
    def setUp(self):
        main.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = main.app.test_client()
        self.current = {"refresh_time": "06:30", "timezone": "Asia/Shanghai"}

    def test_saves_valid_allocation_in_persistent_settings(self):
        with (
            patch.object(main.store, "load_settings", return_value=dict(self.current)),
            patch.object(main.store, "save_settings") as save_settings,
        ):
            response = self.client.post(
                "/settings/allocation",
                data={
                    "weight_dividend_low_vol": "25",
                    "weight_nasdaq100": "45",
                    "weight_gold": "15",
                    "weight_long_bond": "10",
                    "weight_cash": "5",
                },
            )

        self.assertEqual(response.status_code, 302)
        saved = save_settings.call_args.args[0]
        self.assertEqual(saved["refresh_time"], "06:30")
        self.assertAlmostEqual(sum(saved["base_weights"].values()), 1.0)
        self.assertEqual(saved["base_weights"]["nasdaq100"], 0.45)

    def test_rejects_allocation_when_total_is_not_100_percent(self):
        with patch.object(main.store, "save_settings") as save_settings:
            response = self.client.post(
                "/settings/allocation",
                data={
                    "weight_dividend_low_vol": "30",
                    "weight_nasdaq100": "40",
                    "weight_gold": "10",
                    "weight_long_bond": "5",
                    "weight_cash": "5",
                },
            )

        self.assertEqual(response.status_code, 302)
        save_settings.assert_not_called()

    def test_refresh_time_update_preserves_saved_allocation(self):
        current = {
            **self.current,
            "base_weights": {
                "dividend_low_vol": 0.25,
                "nasdaq100": 0.45,
                "gold": 0.15,
                "long_bond": 0.10,
                "cash": 0.05,
            },
        }
        with (
            patch.object(main.store, "load_settings", return_value=current),
            patch.object(main.store, "save_settings") as save_settings,
        ):
            response = self.client.post("/settings", data={"refresh_time": "08:45"})

        self.assertEqual(response.status_code, 302)
        saved = save_settings.call_args.args[0]
        self.assertEqual(saved["refresh_time"], "08:45")
        self.assertEqual(saved["base_weights"], current["base_weights"])

    def test_housing_settings_are_validated_and_preserve_allocation(self):
        current = {**self.current, "base_weights": {"nasdaq100": 0.5}}
        with (
            patch.object(main.store, "load_settings", return_value=current),
            patch.object(main.store, "save_settings") as save,
        ):
            response = self.client.post("/settings/housing", data={
                "housing_enabled": "on", "purchase_month": "2028-06", "target_amount": "1,000,000",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(save.call_args.args[0]["base_weights"], {"nasdaq100": 0.5})
        self.assertEqual(save.call_args.args[0]["housing"], {
            "enabled": True, "purchase_month": "2028-06", "target_amount": 1000000,
        })

    def test_invalid_housing_cannot_replace_saved_plan(self):
        for data in (
            {"housing_enabled": "on", "purchase_month": "", "target_amount": "1000"},
            {"housing_enabled": "on", "purchase_month": "2028-06", "target_amount": "nan"},
            {"housing_enabled": "on", "purchase_month": "2028-06", "target_amount": "-1"},
        ):
            with self.subTest(data=data), patch.object(main.store, "save_settings") as save:
                self.client.post("/settings/housing", data=data)
                save.assert_not_called()

    def test_holdings_only_require_five_assets_and_ignore_legacy_reserve(self):
        values = main._parse_holdings({
            "holding_cash": "1000", "holding_long_bond": "100", "housing_reserved": "200",
        })
        self.assertEqual(values["long_bond"], 100)
        self.assertNotIn("housing_reserved", values)

    def test_settings_page_renders_housing_schedule(self):
        current = {**self.current, "housing": {
            "enabled": True, "purchase_month": "2028-06", "target_amount": 120000,
        }}
        with patch.object(main.store, "load_settings", return_value=current):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("2027-06", text)
        self.assertIn("60,000.00", text)
        self.assertLess(text.index('id="weight_nasdaq100"'), text.index('id="weight_dividend_low_vol"'))

    def test_calculate_renders_housing_transfer_and_saves_actual_reserve(self):
        current = {**self.current, "housing": {
            "enabled": True, "purchase_month": "2027-11", "target_amount": 120000,
        }}
        series = pd.Series(100.0, index=pd.bdate_range("2024-01-01", "2026-11-10"))
        with (
            patch.object(main, "store") as store,
            patch.object(main, "_today", return_value=pd.Timestamp("2026-11-11").date()),
        ):
            store.load_settings.return_value = current
            store.read_series.return_value = series
            response = self.client.post("/calculate", data={
                "holding_nasdaq100": "100000", "holding_long_bond": "50000",
                "holding_cash": "50000", "housing_reserved": "50000",
            })
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("housing_reserved", store.save_portfolio_input.call_args.args[0])
            result = store.save_calculation.call_args.args[0]
            self.assertEqual(result["housing"]["transfer_amount"], 10000)
            self.assertEqual(result["investment_total"], 140000)
        text = response.get_data(as_text=True)
        self.assertIn("买房资金提取指引", text)
        self.assertIn("<strong>购房资金</strong>", text)
        self.assertEqual(result["rows"][-1]["asset"], "housing")
        self.assertEqual(result["housing"]["bond_total_target"], 67000)
        self.assertIn("+10,000.00", text)
        self.assertNotIn('name="housing_reserved"', text)
        self.assertIn("<td>按购房计划累计预留 · 长期国债</td>", text)
        self.assertIn("<td>基础仓位 + 红利/黄金转入</td>", text)
        self.assertNotIn("基础仓位 + 红利/黄金转入 + 购房专款", text)

    def test_disabled_housing_hides_every_housing_change_on_calculation_page(self):
        series = pd.Series(100.0, index=pd.bdate_range("2024-01-01", "2026-11-10"))
        current = {**self.current, "housing": {
            "enabled": False, "purchase_month": "2027-11", "target_amount": 120000,
        }}
        with (
            patch.object(main, "store") as store,
            patch.object(main, "_today", return_value=pd.Timestamp("2026-11-11").date()),
        ):
            store.load_settings.return_value = current
            store.read_series.return_value = series
            store.load_portfolio_input.return_value = {
                "long_bond": 50000, "cash": 150000, "housing_reserved": 50000,
            }
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for phrase in ("购房", "买房", "专款", "housing_reserved"):
            self.assertNotIn(phrase, text)
        self.assertEqual(text.count("<td>基础仓位 + 红利/黄金转入</td>"), 2)

    def test_enabled_housing_before_first_withdrawal_keeps_ordinary_bond_allocation(self):
        current = {**self.current, "housing": {
            "enabled": True, "purchase_month": "2028-11", "target_amount": 120000,
        }}
        series = pd.Series(100.0, index=pd.bdate_range("2024-01-01", "2026-11-10"))
        with (
            patch.object(main, "store") as store,
            patch.object(main, "_today", return_value=pd.Timestamp("2026-11-11").date()),
        ):
            store.load_settings.return_value = current
            store.read_series.return_value = series
            response = self.client.post("/calculate", data={
                "holding_long_bond": "50000", "holding_cash": "150000",
            })
            result = store.save_calculation.call_args.args[0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["housing"]["current_reserved"], 0)
        self.assertEqual(result["housing"]["transfer_amount"], 0)
        self.assertEqual(result["housing"]["bond_total_target"], 10000)
        bond = next(row for row in result["rows"] if row["asset"] == "long_bond")
        self.assertEqual(bond["current_value"], 50000)
        self.assertEqual(bond["target_value"], 10000)
        self.assertEqual(result["rows"][-1]["target_value"], 0)
        self.assertIn("<td>基础仓位 + 红利/黄金转入</td>", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
