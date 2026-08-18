# Daily/monthly backtest environment

This directory is an independent research run. It does not change the existing
monthly strategy engine or the web application's live calculation.

The default command loads one official-data snapshot and runs four comparable
scenarios: daily/monthly signal checks crossed with `sell_all`/`sell_half`.
`frequency_comparison_raw_signal_monthly_10000.csv` compares the two frequencies
using the same snapshot and monthly cash-flow schedule.

The default run checks completed market days and uses each sleeve's raw
close/NAV/index level for both the signal price and MA250. QQQ uses the v1test-compatible
Yahoo Finance chart API (raw close for trading, adjusted close as the
total-return reference); Nasdaq's QQQ history is fetched separately as an
overlap cross-check. A sell requires two
consecutive own trading days below `MA250 * 0.97`; a buy requires two above
`MA250 * 1.03`. Execution is on the next available trading close. There is
no additional position-drift gate: every confirmed sell signal is executed.
`sell_all` sets the post-sale target to 0 and `sell_half` sets it to half of
the base target (for example, 30% becomes 15%), even when price drift made the
pre-trade weight slightly different. The `--released-to-cash-ratio` option
controls the split of net sale proceeds: `0` sends only the actual amount
released by the sale to the long-bond sleeve, while `0.5` is the default
half-cash/half-bond split. Existing cash is not used for that sale-funded bond
purchase. Ordinary buys preserve the target cash sleeve (normally 5% of NAV).
The default run adds CNY 10,000 on the first available portfolio trading day of
each calendar month, then deploys the new cash to the current target weights.

The monthly comparison keeps the same daily MA250 and the same two-own-trading-
day confirmation. It only evaluates at the last available portfolio date of each
calendar month, using the two most recent own trading days for confirmation;
execution remains on the next available trading close. `monthly_10th` instead
checks the completed trading day immediately before the first trading day on or
after the 10th and executes on that next trading day. This makes the monthly
and daily runs differ only in signal-check/execution schedule. The default
`--signal-basis raw_price` uses the previous completed raw close/NAV/index level
against MA250 for the signal; the final NAV still credits the verified dividend
cash ledger. `--signal-basis total_return` is available as an alternative signal
comparison.

The initial portfolio is CNY 50,000 at the first completed close; the initial
allocation itself has no simulated commission. Later orders use 0.006% with a
CNY 0.30 minimum commission. The annual return and annual maximum drawdown
reports use time-weighted returns, excluding the monthly external contributions;
the CSV also keeps drawdown from the all-time TWR peak as a separate column.

Raw closes are used for trades and market value. In the default raw-signal
scenario, dividends are ignored only by the signal and MA250. Verified
distributions create ex-date entitlements and are credited as cash on the
official payment date; QQQ dividends are handled in USD before USD/CNY
conversion and domestic ETF distributions directly in CNY. For QQQ events
that predate the official Nasdaq dividend table, Yahoo supplies ex-date and
amount but no payment date; the audit manifest marks these rows and credits
cash on the ex-date rather than silently dropping them.
GLD is marked `verified_no_distribution` only after the issuer's official
statement is downloaded and checked; an unavailable or unparseable statement
pauses its signal.

The generated `data/source_manifest.json`, `data/dividends.csv`, and result CSV
files record source URLs, checked timestamps and SHA-256 digests. A warning in
the manifest marks the affected dividend cash result and is not silently treated
as a verified total return. In the raw-signal run it does not pause the raw-price
signal; in the dividend-adjusted signal comparison it pauses that asset.
The low-volatility sleeve uses the official CSI H20269 total-return index
history. H20269 embeds distributions in the index level; it is not an ETF NAV,
and no separate ETF cash dividend is booked for this sleeve. MA250 is calculated
from the freshly fetched 250-session history.

Use `--start-date YYYY-MM-DD` to choose the backtest start. For example,
`--start-date 2006-01-01` uses Yahoo QQQ history from 2005-01-03 onward and
H20269 history from the official CSI endpoint. Output names include `_from_YYYYMMDD`; annual
tables include the maximum-drawdown peak, trough, recovery date and recovery
days.

The 2016-08-15 to 2017-08-23 long-bond sleeve is a transparent, scale-stitched
historical ten-year-bond proxy because 511260 did not yet have ETF observations;
the long-bond asset return table starts at the first official 511260 close
(2017-08-24). This proxy is not presented as an ETF price.

Run from the project directory:

```text
python daily_backtest/daily_backtest.py
```

Use `--frequency daily`, `--frequency monthly`, or `--frequency monthly_10th` to
run one side only. Use
`--signal-basis total_return` for the dividend-adjusted signal comparison. Use
`--monthly-contribution 0` to remove the monthly cash flow. The default
`--frequency both --signal-basis raw_price --monthly-contribution 10000` writes
`results/strategy_summary_raw_signal_monthly_10000_from_20160815.csv`,
`results/frequency_comparison_raw_signal_monthly_10000_from_20160815.csv`, and
`results/annual_strategy_raw_signal_monthly_10000_from_20160815.csv`.

Use `--low-vol-source h20269` (default) for the CSI H20269 total-return index,
or `--low-vol-source 512890` for the SSE ETF raw-close comparison. Use
`--confirmation-days 1` for a single completed trading-day confirmation;
the default is two days. Use `--released-to-cash-ratio 0` to send all released
money to long bonds.
