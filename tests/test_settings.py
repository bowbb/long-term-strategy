from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
