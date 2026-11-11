from __future__ import annotations

import math
import re
from typing import Any, Mapping

import pandas as pd


DEFAULT_HOUSING = {"enabled": False, "purchase_month": "", "target_amount": 0.0}


def validate_housing(values: Mapping[str, Any]) -> dict[str, Any]:
    enabled = values.get("enabled") in (True, "on", "1")
    purchase_month = str(values.get("purchase_month") or "").strip()
    try:
        amount = float(values.get("target_amount", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("计划购房金额必须是有效数字") from exc
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("计划购房金额必须是非负的有限金额")
    if enabled or purchase_month:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", purchase_month):
            raise ValueError("请选择有效的购房年份和月份")
        if not 1901 <= int(purchase_month[:4]) <= 9998:
            raise ValueError("购房年份必须在 1901 到 9998 之间")
    amount = round(amount, 2)
    if enabled and amount <= 0:
        raise ValueError("启用买房计划时，计划购房金额必须大于0")
    return {"enabled": enabled, "purchase_month": purchase_month, "target_amount": round(amount, 2)}


def housing_schedule(config: Mapping[str, Any] | None, asof: Any) -> dict[str, Any]:
    plan = validate_housing(config or DEFAULT_HOUSING)
    plan.update({"schedule": [], "required_amount": 0.0, "start_month": None,
                 "monthly_amount": 0.0, "next_month": None, "phase": "disabled"})
    if not plan["enabled"]:
        return plan

    purchase = pd.Period(plan["purchase_month"], freq="M")
    start = purchase - 12
    current = pd.Period(pd.Timestamp(asof), freq="M")
    total_cents = round(plan["target_amount"] * 100)
    first_cents = total_cents // 2
    remaining_cents = total_cents - first_cents
    cumulative = 0
    for step in range(13):
        month = start + step
        cents = first_cents if step == 0 else (
            remaining_cents * step // 12 - remaining_cents * (step - 1) // 12
        )
        cumulative += cents
        plan["schedule"].append({
            "month": str(month), "amount": cents / 100, "cumulative": cumulative / 100,
            "due": month <= current,
        })
        if month <= current:
            plan["required_amount"] = cumulative / 100
        elif plan["next_month"] is None:
            plan["next_month"] = str(month)
    plan.update({
        "start_month": str(start),
        "monthly_amount": remaining_cents / 1200,
        "phase": "waiting" if current < start else "purchase" if current >= purchase else "reserving",
    })
    return plan


def housing_transfer_plan(
    config: Mapping[str, Any] | None, asof: Any, total: float, bond_value: float,
) -> dict[str, Any]:
    plan = housing_schedule(config, asof)
    target = min(plan["required_amount"], total)
    # The scheduled amount is the housing sleeve; any bonds above it belong
    # to the ordinary strategy. A short bond holding is never made negative.
    reserved = min(bond_value, target)
    transfer = max(target - reserved, 0.0)
    plan.update({
        "current_reserved": reserved,
        "transfer_amount": transfer,
        "target_reserved": target,
        "shortfall": max(plan["required_amount"] - target, 0.0),
        "investment_total": max(total - target, 0.0),
    })
    return plan
