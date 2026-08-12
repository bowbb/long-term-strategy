from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from .market_data import ASSET_META, MarketStore


APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = APP_ROOT / "strategy_config.json"

RISK_ASSETS = (
    "csi300",
    "dividend_low_vol",
    "star50",
    "nasdaq100",
    "gold",
)
MARKET_ASSETS = RISK_ASSETS + ("long_bond",)
ALL_ASSETS = MARKET_ASSETS + ("cash",)
ETF_ASSETS = MARKET_ASSETS


def _asset_label(asset: str) -> str:
    return str(ASSET_META[asset]["label"])


def load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _trend_snapshot(
    series: pd.Series,
    slow_days: int,
    hysteresis_band: float,
    previous_multiplier: Optional[float],
) -> Dict[str, Any]:
    observed = series.dropna().astype(float)
    result: Dict[str, Any] = {
        "available": False,
        "reason": "insufficient_history",
        "latest": None,
        "ma250": None,
        "lower_bound": None,
        "upper_bound": None,
        "observations": int(len(observed)),
        "multiplier": 0.0,
        "action": "insufficient_history",
    }
    if observed.empty:
        return result

    result["latest"] = float(observed.iloc[-1])
    if len(observed) < slow_days:
        return result

    latest = float(observed.iloc[-1])
    ma250 = float(observed.iloc[-slow_days:].mean())
    lower_bound = ma250 * (1.0 - hysteresis_band)
    upper_bound = ma250 * (1.0 + hysteresis_band)

    if latest > upper_bound:
        multiplier = 1.0
        action = "switch_on"
    elif latest < lower_bound:
        multiplier = 0.0
        action = "switch_off"
    elif previous_multiplier is None:
        multiplier = 1.0 if latest > ma250 else 0.0
        action = "initialize_from_ma250"
    else:
        multiplier = float(previous_multiplier)
        action = "hold_previous"

    result.update(
        {
            "available": True,
            "reason": None,
            "ma250": ma250,
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "multiplier": multiplier,
            "action": action,
        }
    )
    return result


def _month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    if index.empty:
        return []
    dates = pd.DatetimeIndex(index).sort_values().unique()
    return [pd.Timestamp(group.iloc[-1]) for _, group in pd.Series(dates, index=dates).groupby(dates.to_period("M"))]


def _signal_context(store: MarketStore, asof: pd.Timestamp) -> tuple[pd.Timestamp, Optional[pd.Timestamp]]:
    calendar = store.read_series("csi300").sort_index().index
    completed_month_cutoff = asof.to_period("M").start_time - pd.Timedelta(days=1)
    eligible = calendar[calendar <= completed_month_cutoff]
    if eligible.empty:
        raise RuntimeError("沪深300日历中没有上一个完整月份的数据，无法生成月末信号。")

    signal_date = pd.Timestamp(eligible[-1]).normalize()
    execution_dates = calendar[calendar > signal_date]
    execution_date = pd.Timestamp(execution_dates[0]).normalize() if len(execution_dates) else None
    return signal_date, execution_date


def _aligned_prices(store: MarketStore, signal_date: pd.Timestamp) -> pd.DataFrame:
    calendar = store.read_series("csi300").sort_index()
    calendar = calendar.loc[calendar.index <= signal_date].index
    if calendar.empty:
        raise RuntimeError("沪深300交易日历为空，无法生成策略信号。")

    prices = pd.DataFrame(index=calendar)
    for asset in MARKET_ASSETS:
        series = store.read_series(asset).sort_index()
        series = series.loc[series.index <= signal_date]
        prices[asset] = series.reindex(calendar).ffill()
    return prices


def _replay_hysteresis(
    prices: pd.DataFrame,
    signal_date: pd.Timestamp,
    slow_days: int,
    hysteresis_band: float,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, float]]:
    states: Dict[str, float] = {}
    final_snapshots: Dict[str, Dict[str, Any]] = {}

    for month_end in _month_end_dates(prices.index[prices.index <= signal_date]):
        for asset in RISK_ASSETS:
            snapshot = _trend_snapshot(
                prices.loc[:month_end, asset],
                slow_days=slow_days,
                hysteresis_band=hysteresis_band,
                previous_multiplier=states.get(asset),
            )
            if snapshot["available"]:
                states[asset] = float(snapshot["multiplier"])
            if month_end == signal_date:
                final_snapshots[asset] = snapshot

    if not final_snapshots:
        raise RuntimeError("未找到可用的完整月末信号。")
    return final_snapshots, states


def _market_snapshot(store: MarketStore, asset: str, asof: pd.Timestamp) -> Dict[str, Any]:
    config = load_config()
    slow_days = int(config["slow_ma_days"])
    hysteresis_band = float(config["hysteresis_band"])
    try:
        series = store.read_series(asset).sort_index()
    except Exception as exc:
        return {
            "asset": asset,
            "name": _asset_label(asset),
            "error": str(exc),
            "latest": None,
            "latest_date": None,
            "stale": True,
            "ma250": None,
            "lower_bound": None,
            "upper_bound": None,
            "observations": 0,
        }

    series = series.loc[series.index <= asof]
    if series.empty:
        return {
            "asset": asset,
            "name": _asset_label(asset),
            "error": "所选日期之前没有数据",
            "latest": None,
            "latest_date": None,
            "stale": True,
            "ma250": None,
            "lower_bound": None,
            "upper_bound": None,
            "observations": 0,
        }

    trend = _trend_snapshot(series, slow_days, hysteresis_band, None)
    latest_date = pd.Timestamp(series.index[-1]).normalize()
    return {
        "asset": asset,
        "name": _asset_label(asset),
        "error": None,
        "latest": float(series.iloc[-1]),
        "latest_date": latest_date.date().isoformat(),
        "stale": bool(latest_date < asof.normalize()),
        "ma250": trend["ma250"],
        "lower_bound": trend["lower_bound"],
        "upper_bound": trend["upper_bound"],
        "observations": trend["observations"],
    }


def build_market_overview(store: MarketStore, asof: Any) -> list[Dict[str, Any]]:
    timestamp = pd.Timestamp(asof).normalize()
    return [_market_snapshot(store, asset, timestamp) for asset in MARKET_ASSETS]


def _commission(trade_value: float, rate: float, minimum: float) -> float:
    if trade_value <= 0.01:
        return 0.0
    return max(trade_value * rate, minimum)


def calculate_plan(
    store: MarketStore,
    holdings: Mapping[str, Any],
    asof: Any,
) -> Dict[str, Any]:
    config = load_config()
    timestamp = pd.Timestamp(asof).normalize()
    slow_days = int(config["slow_ma_days"])
    hysteresis_band = float(config["hysteresis_band"])
    release_to_cash = float(config["released_to_cash_ratio"])
    release_to_bond = 1.0 - release_to_cash
    commission_rate = float(config["commission_rate"])
    minimum_commission = float(config["minimum_commission"])
    base_weights = {asset: float(config["base_weights"][asset]) for asset in ALL_ASSETS}

    signal_date, execution_date = _signal_context(store, timestamp)
    prices = _aligned_prices(store, signal_date)
    final_snapshots, _ = _replay_hysteresis(
        prices,
        signal_date,
        slow_days=slow_days,
        hysteresis_band=hysteresis_band,
    )

    target_weights = dict(base_weights)
    released_weight = 0.0
    unavailable_weight = 0.0
    process: list[Dict[str, Any]] = []

    for asset in RISK_ASSETS:
        base = base_weights[asset]
        trend = final_snapshots[asset]
        if not trend["available"]:
            target_weights[asset] = 0.0
            target_weights["long_bond"] += base
            unavailable_weight += base
            process.append(
                {
                    "asset": asset,
                    "name": _asset_label(asset),
                    "base_weight": base,
                    "multiplier": 0.0,
                    "released_weight": base,
                    "destination": "历史不足250个交易日，基础权重全部转入境内长期国债",
                    "action": "insufficient_history",
                }
            )
            continue

        multiplier = float(trend["multiplier"])
        target = base * multiplier
        released = base - target
        target_weights[asset] = target
        target_weights["long_bond"] += released * release_to_bond
        target_weights["cash"] += released * release_to_cash
        released_weight += released
        process.append(
            {
                "asset": asset,
                "name": _asset_label(asset),
                "base_weight": base,
                "multiplier": multiplier,
                "released_weight": released,
                "destination": "释放部分各50%转入境内长期国债和人民币现金",
                "action": trend["action"],
            }
        )

    total_weight = sum(target_weights.values())
    if total_weight <= 0:
        raise RuntimeError("目标权重计算失败。")
    target_weights = {asset: weight / total_weight for asset, weight in target_weights.items()}

    normalized_holdings = {asset: _as_float(holdings.get(asset, 0.0)) for asset in ALL_ASSETS}
    current_total = sum(normalized_holdings.values())
    rows: list[Dict[str, Any]] = []
    total_commission = 0.0
    action_labels = {
        "switch_on": "高于上轨，持有",
        "switch_off": "低于下轨，空仓",
        "hold_previous": "滞回区间内，维持上月状态",
        "initialize_from_ma250": "首次进入滞回区间，按MA250初始化",
        "insufficient_history": "历史不足250日",
    }

    for asset in ALL_ASSETS:
        target_weight = target_weights[asset]
        target_value = current_total * target_weight
        current_value = normalized_holdings[asset]
        trade_value = target_value - current_value
        fee = _commission(abs(trade_value), commission_rate, minimum_commission) if asset in ETF_ASSETS else 0.0
        total_commission += fee

        if asset in RISK_ASSETS:
            trend = final_snapshots[asset]
            market = {
                "latest": trend["latest"],
                "ma250": trend["ma250"],
                "lower_bound": trend["lower_bound"],
                "upper_bound": trend["upper_bound"],
                "observations": trend["observations"],
                "available": trend["available"],
                "multiplier": trend["multiplier"],
                "action": trend["action"],
            }
            signal_text = action_labels[trend["action"]]
        elif asset == "long_bond":
            market = _market_snapshot(store, asset, signal_date)
            market.update({"available": True, "multiplier": None, "action": "defensive_asset"})
            signal_text = "基础仓位 + 风险资产转入"
        else:
            market = {
                "latest": 1.0,
                "ma250": None,
                "lower_bound": None,
                "upper_bound": None,
                "observations": None,
                "available": True,
                "multiplier": None,
                "action": "cash_reserve",
            }
            signal_text = "基础仓位 + 风险资产转入"

        rows.append(
            {
                "asset": asset,
                "name": _asset_label(asset),
                "current_value": current_value,
                "current_weight": current_value / current_total if current_total > 0 else 0.0,
                "target_weight": target_weight,
                "target_value": target_value,
                "trade_value": trade_value,
                "trade_action": "增加" if trade_value > 0.01 else "减少" if trade_value < -0.01 else "不变",
                "commission": fee,
                "signal_text": signal_text,
                "market": market,
            }
        )

    return {
        "calculation_date": timestamp.date().isoformat(),
        "signal_date": signal_date.date().isoformat(),
        "execution_date": execution_date.date().isoformat() if execution_date is not None else None,
        "current_total": current_total,
        "target_weights": target_weights,
        "rows": rows,
        "process": process,
        "released_weight": released_weight,
        "unavailable_weight": unavailable_weight,
        "estimated_commission": total_commission,
        "strategy": {
            "slow_ma_days": slow_days,
            "hysteresis_band": hysteresis_band,
            "released_to_cash_ratio": release_to_cash,
            "released_to_bond_ratio": release_to_bond,
            "commission_rate": commission_rate,
            "minimum_commission": minimum_commission,
            "base_weights": base_weights,
        },
    }
