from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .market_data import ASSET_META, MarketStore


APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "app" / "strategy_config.json"
RISK_ASSETS = ("csi300", "dividend_low_vol", "star50", "nasdaq100", "gold")
ALL_ASSETS = RISK_ASSETS + ("long_bond", "cash")


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _as_float(value: object) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _trend_snapshot(series: pd.Series, fast_days: int, slow_days: int) -> dict[str, Any]:
    observed = pd.to_numeric(series, errors="coerce").dropna()
    result: dict[str, Any] = {
        "available": False,
        "reason": "有效历史不足250个交易日",
        "observations": int(len(observed)),
        "latest": None,
        "latest_date": None,
        "fast_ma": None,
        "slow_ma": None,
        "conditions": 0,
        "multiplier": 0.0,
    }
    if not observed.empty:
        result["latest"] = float(observed.iloc[-1])
        result["latest_date"] = pd.Timestamp(observed.index[-1]).strftime("%Y-%m-%d")
    if len(observed) < slow_days:
        return result
    latest = float(observed.iloc[-1])
    fast_ma = float(observed.iloc[-fast_days:].mean())
    slow_ma = float(observed.iloc[-slow_days:].mean())
    conditions = int(latest > slow_ma) + int(fast_ma > slow_ma)
    return {
        "available": True,
        "reason": "有效",
        "observations": int(len(observed)),
        "latest": latest,
        "latest_date": pd.Timestamp(observed.index[-1]).strftime("%Y-%m-%d"),
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "conditions": conditions,
        "multiplier": (0.0, 0.50, 1.0)[conditions],
    }


def _market_snapshot(store: MarketStore, asset: str, asof: pd.Timestamp) -> dict[str, Any]:
    try:
        series = store.read_series(asset)
        series = series.loc[series.index <= asof]
        if series.empty:
            raise ValueError("计算日之前没有有效数据")
        trend = _trend_snapshot(series, 20, 250)
        latest_date = pd.Timestamp(series.index[-1])
        return {
            "latest": float(series.iloc[-1]),
            "latest_date": latest_date.strftime("%Y-%m-%d"),
            "stale_days": max((asof - latest_date).days, 0),
            "ma20": _as_float(trend["fast_ma"]),
            "ma250": _as_float(trend["slow_ma"]),
            "observations": int(len(series)),
            "trend": trend,
            "error": None,
        }
    except Exception as exc:
        return {
            "latest": None,
            "latest_date": None,
            "stale_days": None,
            "ma20": None,
            "ma250": None,
            "observations": 0,
            "trend": _trend_snapshot(pd.Series(dtype=float), 20, 250),
            "error": str(exc),
        }


def build_market_overview(store: MarketStore, asof: date | pd.Timestamp) -> list[dict[str, Any]]:
    timestamp = pd.Timestamp(asof)
    return [
        {
            "asset": asset,
            "label": ASSET_META[asset]["label"],
            "source": ASSET_META[asset]["source"],
            **_market_snapshot(store, asset, timestamp),
        }
        for asset in ALL_ASSETS
    ]


def calculate_plan(
    store: MarketStore,
    holdings: dict[str, float],
    asof: date | pd.Timestamp,
) -> dict[str, Any]:
    config = load_config()
    timestamp = pd.Timestamp(asof).normalize()
    base_weights = {asset: float(value) for asset, value in config["base_weights"].items()}
    weights = dict(base_weights)
    details: dict[str, dict[str, Any]] = {}
    process: list[str] = [
        f"计算日：{timestamp.strftime('%Y-%m-%d')}（使用不晚于该日的最新本地数据）",
        "先从基础配置 15%/15%/10%/40%/10%/5%/5% 开始。",
    ]

    for asset in RISK_ASSETS:
        snapshot = _market_snapshot(store, asset, timestamp)
        trend = snapshot["trend"]
        base = base_weights[asset]
        if not trend["available"]:
            weights[asset] = 0.0
            weights["long_bond"] += base
            process.append(
                f"{ASSET_META[asset]['label']}：有效历史不足250日，基础权重 {base:.1%} 暂转境内长期国债。"
            )
            details[asset] = {
                "base_weight": base,
                "target_weight": 0.0,
                "released": base,
                "release_to_cash": 0.0,
                "release_to_bond": base,
                "market": snapshot,
            }
            continue

        score = int(trend["conditions"])
        multiplier = float(trend["multiplier"])
        target = base * multiplier
        released = base - target
        to_cash = released * float(config["released_to_cash_ratio"])
        to_bond = released - to_cash
        weights[asset] = target
        weights["cash"] += to_cash
        weights["long_bond"] += to_bond
        condition_text = f"价格>{trend['slow_ma']:.4g}" if trend["latest"] > trend["slow_ma"] else f"价格<={trend['slow_ma']:.4g}"
        ma_text = f"MA20>{trend['slow_ma']:.4g}" if trend["fast_ma"] > trend["slow_ma"] else f"MA20<={trend['slow_ma']:.4g}"
        process.append(
            f"{ASSET_META[asset]['label']}：得分 {score}/2（{condition_text}，{ma_text}），"
            f"系数 {multiplier:.0%}，目标 {target:.1%}；释放 {released:.1%}，现金/长期国债各分 {to_cash:.1%}/{to_bond:.1%}。"
        )
        details[asset] = {
            "base_weight": base,
            "target_weight": target,
            "released": released,
            "release_to_cash": to_cash,
            "release_to_bond": to_bond,
            "market": snapshot,
        }

    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("目标权重总和为零")
    weights = {asset: value / total_weight for asset, value in weights.items()}
    process.append("最后把目标权重归一化到100%；ETF 溢价/折价不作为信号。")

    clean_holdings = {asset: max(float(holdings.get(asset, 0.0)), 0.0) for asset in ALL_ASSETS}
    current_total = sum(clean_holdings.values())
    if current_total <= 0:
        raise ValueError("当前持仓和现金合计必须大于0")

    rows: list[dict[str, Any]] = []
    for asset in ALL_ASSETS:
        current_value = clean_holdings[asset]
        target_weight = float(weights.get(asset, 0.0))
        target_value = current_total * target_weight
        delta_value = target_value - current_value
        action = "增加" if delta_value > 0.01 else "减少" if delta_value < -0.01 else "保持"
        market = details.get(asset, {}).get("market") or _market_snapshot(store, asset, timestamp)
        if asset in {"long_bond", "cash"} and asset not in details:
            details[asset] = {"base_weight": base_weights[asset], "target_weight": target_weight, "market": market}
        rows.append(
            {
                "asset": asset,
                "label": ASSET_META[asset]["label"],
                "source": ASSET_META[asset]["source"],
                "base_weight": base_weights[asset],
                "target_weight": target_weight,
                "current_value": current_value,
                "current_weight": current_value / current_total,
                "target_value": target_value,
                "delta_value": delta_value,
                "action": action,
                "latest": market.get("latest"),
                "latest_date": market.get("latest_date"),
                "ma20": market.get("ma20"),
                "ma250": market.get("ma250"),
                "observations": market.get("observations", 0),
                "trend": details.get(asset, {}).get("market", {}).get("trend", {}),
                "error": market.get("error"),
            }
        )

    process.append(
        f"当前总资产 {current_total:,.2f} 元；目标金额按当前总资产计算，现金输入应包含本次准备投入的工资。"
    )
    return {
        "calculation_date": timestamp.strftime("%Y-%m-%d"),
        "current_total": current_total,
        "target_total": current_total,
        "non_cash_increase": sum(max(row["delta_value"], 0.0) for row in rows if row["asset"] != "cash"),
        "non_cash_decrease": sum(max(-row["delta_value"], 0.0) for row in rows if row["asset"] != "cash"),
        "cash_change": next(row["delta_value"] for row in rows if row["asset"] == "cash"),
        "rows": rows,
        "process": process,
        "strategy": {
            "base_weights": base_weights,
            "fast_ma_days": 20,
            "slow_ma_days": 250,
            "risk_multipliers": {"0 conditions": 0.0, "1 condition": 0.5, "2 conditions": 1.0},
            "released_to_cash_ratio": float(config["released_to_cash_ratio"]),
            "fallback": "有效历史不足250个交易日的风险资产转入境内长期国债",
        },
    }
