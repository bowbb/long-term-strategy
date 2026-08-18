from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from .market_data import ASSET_META, MarketStore


APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = APP_ROOT / "strategy_config.json"

RISK_ASSETS = (
    "dividend_low_vol",
    "nasdaq100",
    "gold",
)
MARKET_ASSETS = RISK_ASSETS + ("long_bond",)
ALL_ASSETS = MARKET_ASSETS + ("cash",)
ETF_ASSETS = MARKET_ASSETS
CALENDAR_ASSET = "dividend_low_vol"


def _asset_label(asset: str) -> str:
    return str(ASSET_META[asset]["label"])


def load_config() -> Dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stored_strategy_multipliers(store: MarketStore) -> dict[str, float]:
    """Read the last calculated sleeve states when the store supports it."""
    loader = getattr(store, "load_calculation", None)
    if not callable(loader):
        return {}
    try:
        calculation = loader()
    except Exception:
        return {}
    rows = calculation.get("rows", []) if isinstance(calculation, dict) else []
    stored: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("asset") not in RISK_ASSETS:
            continue
        market = row.get("market")
        multiplier = market.get("multiplier") if isinstance(market, dict) else None
        try:
            value = float(multiplier)
        except (TypeError, ValueError):
            continue
        if value in {0.0, 0.5, 1.0}:
            stored[str(row["asset"])] = value
    return stored


def _previous_multiplier(
    store: MarketStore,
    asset: str,
    holding: float,
    current_total: float,
    base_weight: float,
) -> float:
    """Infer the current state without turning a half position back to full."""
    if holding <= 0.01 or current_total <= 0.0:
        return 0.0

    stored = _stored_strategy_multipliers(store).get(asset)
    if stored in {0.5, 1.0}:
        return stored

    current_weight = holding / current_total
    return 1.0 if current_weight >= base_weight * 0.75 else 0.5


def _trend_snapshot(
    series: pd.Series,
    slow_days: int,
    hysteresis_band: float,
    previous_multiplier: Optional[float],
    sell_multiplier: float = 0.5,
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
        multiplier = sell_multiplier if previous_multiplier != 0.0 else 0.0
        action = "switch_to_half" if multiplier else "stay_off_below_band"
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


def _signal_context(store: MarketStore, asof: pd.Timestamp) -> tuple[pd.Timestamp, Optional[pd.Timestamp]]:
    calendar = store.read_series(CALENDAR_ASSET).sort_index().index
    eligible = calendar[calendar < asof]
    if eligible.empty:
        raise RuntimeError("红利低波交易日历中没有早于计算日的已完成数据，无法生成信号。")

    signal_date = pd.Timestamp(eligible[-1]).normalize()
    execution_date = asof.normalize()
    return signal_date, execution_date


def _aligned_prices(store: MarketStore, signal_date: pd.Timestamp) -> pd.DataFrame:
    calendar = store.read_series(CALENDAR_ASSET).sort_index()
    calendar = calendar.loc[calendar.index <= signal_date].index
    if calendar.empty:
        raise RuntimeError("红利低波交易日历为空，无法生成策略信号。")

    prices = pd.DataFrame(index=calendar)
    for asset in MARKET_ASSETS:
        series = store.read_series(asset).sort_index()
        series = series.loc[series.index <= signal_date]
        prices[asset] = series.reindex(calendar).ffill()
    return prices


def _market_snapshot(store: MarketStore, asset: str, asof: pd.Timestamp) -> Dict[str, Any]:
    config = load_config()
    slow_days = int(config["slow_ma_days"])
    hysteresis_band = float(config["hysteresis_band"])
    sell_multiplier = float(config.get("sell_signal_multiplier", 0.5))
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

    trend = _trend_snapshot(series, slow_days, hysteresis_band, None, sell_multiplier)
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


def _signal_text(trend: Mapping[str, Any]) -> str:
    action = str(trend["action"])
    if action == "switch_on":
        return "突破上轨，恢复基础仓位"
    if action == "switch_to_half":
        return "跌破下轨，降至基础仓位50%"
    if action == "stay_off_below_band":
        return "低于下轨且当前无仓，继续空仓"
    if action == "hold_previous":
        return "滞回区间内，保持当前状态"
    if action == "initialize_from_ma250":
        return "首次状态：持有" if float(trend["multiplier"]) == 1.0 else "首次状态：空仓"
    return "历史不足250日"


def calculate_plan(
    store: MarketStore,
    holdings: Mapping[str, Any],
    asof: Any,
) -> Dict[str, Any]:
    config = load_config()
    timestamp = pd.Timestamp(asof).normalize()
    slow_days = int(config["slow_ma_days"])
    hysteresis_band = float(config["hysteresis_band"])
    sell_multiplier = float(config.get("sell_signal_multiplier", 0.5))
    release_to_cash = float(config["released_to_cash_ratio"])
    release_to_bond = 1.0 - release_to_cash
    commission_rate = float(config["commission_rate"])
    minimum_commission = float(config["minimum_commission"])
    base_weights = {asset: float(config["base_weights"][asset]) for asset in ALL_ASSETS}
    normalized_holdings = {asset: _as_float(holdings.get(asset, 0.0)) for asset in ALL_ASSETS}
    current_total = sum(normalized_holdings.values())
    previous_multipliers = {
        asset: _previous_multiplier(
            store,
            asset,
            normalized_holdings[asset],
            current_total,
            base_weights[asset],
        )
        for asset in RISK_ASSETS
    }

    signal_date, execution_date = _signal_context(store, timestamp)
    prices = _aligned_prices(store, signal_date)
    final_snapshots = {
        asset: _trend_snapshot(
            prices[asset],
            slow_days=slow_days,
            hysteresis_band=hysteresis_band,
            previous_multiplier=previous_multipliers[asset],
            sell_multiplier=sell_multiplier,
        )
        for asset in RISK_ASSETS
    }

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
        if multiplier == 1.0:
            destination = "不释放资金，保持基础仓位"
        elif multiplier == sell_multiplier:
            destination = "释放基础仓位的50%，其中各50%转入境内长期国债和人民币现金"
        else:
            destination = "当前无仓，继续保持空仓"
        process.append(
            {
                "asset": asset,
                "name": _asset_label(asset),
                "base_weight": base,
                "multiplier": multiplier,
                "released_weight": released,
                "destination": destination,
                "action": trend["action"],
            }
        )

    total_weight = sum(target_weights.values())
    if total_weight <= 0:
        raise RuntimeError("目标权重计算失败。")
    target_weights = {asset: weight / total_weight for asset, weight in target_weights.items()}

    rows: list[Dict[str, Any]] = []
    total_commission = 0.0
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
            signal_text = _signal_text(trend)
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
            "sell_signal_multiplier": sell_multiplier,
            "released_to_cash_ratio": release_to_cash,
            "released_to_bond_ratio": release_to_bond,
            "commission_rate": commission_rate,
            "minimum_commission": minimum_commission,
            "base_weights": base_weights,
        },
    }
