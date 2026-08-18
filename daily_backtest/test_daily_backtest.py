import unittest

import pandas as pd

try:
    from daily_backtest.daily_backtest import (
        ALL_ASSETS,
        add_flow_adjusted_metrics,
        annual_metrics_from_nav,
        apply_dividends,
        check_dates_for_frequency,
        drawdown_recovery_metrics,
        monthly_contribution_dates,
        resolve_signal_inputs,
    )
except ImportError:
    from daily_backtest import (
        ALL_ASSETS,
        add_flow_adjusted_metrics,
        annual_metrics_from_nav,
        apply_dividends,
        check_dates_for_frequency,
        drawdown_recovery_metrics,
        monthly_contribution_dates,
        resolve_signal_inputs,
    )


class DividendAdjustmentTests(unittest.TestCase):
    def test_cash_dividend_is_reflected_once_in_backward_adjustment(self) -> None:
        raw = pd.Series(
            [100.0, 90.0, 95.0],
            index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]),
        )
        adjusted, applied = apply_dividends(
            raw,
            [{"ex_date": pd.Timestamp("2020-01-02"), "amount_per_share": 5.0}],
        )
        self.assertAlmostEqual(float(adjusted.iloc[0]), 100.0 * 90.0 / 95.0)
        self.assertAlmostEqual(float(adjusted.iloc[-1]), 95.0)
        self.assertEqual(applied[0]["status"], "applied")

    def test_events_before_history_are_not_applied(self) -> None:
        raw = pd.Series([100.0, 101.0], index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
        adjusted, applied = apply_dividends(
            raw,
            [{"ex_date": pd.Timestamp("2020-01-01"), "amount_per_share": 5.0}],
        )
        pd.testing.assert_series_equal(adjusted, raw)
        self.assertEqual(applied[0]["status"], "before_history_ignored")


class FrequencyComparisonTests(unittest.TestCase):
    def test_monthly_frequency_uses_last_portfolio_date_of_each_month(self) -> None:
        calendar = pd.DatetimeIndex(
            [
                "2020-01-30",
                "2020-01-31",
                "2020-02-28",
                "2020-03-02",
                "2020-03-31",
            ]
        )
        actual = check_dates_for_frequency(calendar, "monthly")
        expected = pd.DatetimeIndex(["2020-01-31", "2020-02-28", "2020-03-31"])
        pd.testing.assert_index_equal(actual, expected)

    def test_monthly_10th_uses_previous_completed_trading_day(self) -> None:
        calendar = pd.DatetimeIndex(
            [
                "2020-01-08",
                "2020-01-09",
                "2020-01-10",
                "2020-01-31",
                "2020-02-07",
                "2020-02-12",
                "2020-02-28",
            ]
        )
        actual = check_dates_for_frequency(calendar, "monthly_10th")
        expected = pd.DatetimeIndex(["2020-01-09", "2020-02-07"])
        pd.testing.assert_index_equal(actual, expected)

    def test_unknown_frequency_is_rejected(self) -> None:
        calendar = pd.DatetimeIndex(["2020-01-02"])
        with self.assertRaises(ValueError):
            check_dates_for_frequency(calendar, "weekly")

    def test_raw_price_basis_uses_raw_signal_but_keeps_cash_events(self) -> None:
        series = pd.Series([100.0], index=pd.to_datetime(["2020-01-02"]))
        data = {
            "raw_cny": {asset: series for asset in ALL_ASSETS},
            "signal_cny": {asset: series * 2 for asset in ALL_ASSETS},
            "events": {asset: [{"amount_per_share": 1.0}] for asset in ALL_ASSETS},
            "event_status": {asset: {"status": "warning"} for asset in ALL_ASSETS},
        }
        signal, events, statuses = resolve_signal_inputs(data, "raw_price")
        self.assertAlmostEqual(float(signal["nasdaq100"].iloc[0]), 100.0)
        self.assertEqual(events["nasdaq100"][0]["amount_per_share"], 1.0)
        self.assertEqual(statuses["dividend_low_vol"]["status"], "verified_raw_price_only")

    def test_monthly_contributions_use_first_available_portfolio_date(self) -> None:
        calendar = pd.DatetimeIndex(["2020-01-02", "2020-01-31", "2020-02-03", "2020-02-28"])
        actual = monthly_contribution_dates(calendar, 10_000.0)
        self.assertEqual(sorted(actual), [pd.Timestamp("2020-01-02"), pd.Timestamp("2020-02-03")])
        self.assertEqual(actual[pd.Timestamp("2020-02-03")], 10_000.0)

    def test_twr_excludes_external_contribution_from_annual_return(self) -> None:
        nav = pd.DataFrame(
            {
                "date": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-02-03"]),
                "nav_cny": [100.0, 210.0, 231.0],
                "contribution_cny": [0.0, 100.0, 0.0],
            }
        )
        enriched = add_flow_adjusted_metrics(nav, 100.0)
        self.assertAlmostEqual(float(enriched["twr_index"].iloc[-1]), 1.21)
        annual = annual_metrics_from_nav(nav, 100.0)
        self.assertAlmostEqual(float(annual.loc[0, "annual_return"]), 0.21)
        self.assertAlmostEqual(float(annual.loc[0, "cumulative_profit_cny"]), 31.0)

    def test_drawdown_recovery_uses_calendar_days(self) -> None:
        dates = pd.to_datetime(["2020-01-01", "2020-01-10", "2020-01-20", "2020-02-01"])
        metrics = drawdown_recovery_metrics(pd.Series([1.0, 0.8, 0.7, 1.0]), dates)
        self.assertEqual(metrics["peak_date"], "2020-01-01")
        self.assertEqual(metrics["trough_date"], "2020-01-20")
        self.assertEqual(metrics["recovery_date"], "2020-02-01")
        self.assertEqual(metrics["recovery_days"], 12)


if __name__ == "__main__":
    unittest.main()
