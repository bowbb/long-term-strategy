from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, flash, redirect, render_template, request, url_for

from .market_data import ASSET_META, MarketStore, refresh_all
from .strategy_engine import ALL_ASSETS, build_market_overview, calculate_plan, effective_base_weights
from .housing import housing_schedule, validate_housing


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "local-strategy-app")
store = MarketStore()
store.ensure_store()
_scheduler_wakeup = threading.Event()
_CN_ZONE = ZoneInfo("Asia/Shanghai")


@app.template_filter("money")
def money(value):
    if value is None:
        return "-"
    return f"{float(value):,.2f}"


@app.template_filter("signed_money")
def signed_money(value):
    if value is None:
        return "-"
    amount = float(value)
    return f"{amount:+,.2f}"


@app.template_filter("pct")
def percentage(value):
    if value is None:
        return "-"
    return f"{float(value):.1%}"


@app.template_filter("number")
def number(value):
    if value is None:
        return "-"
    return f"{float(value):,.4f}"


@app.template_filter("status_text")
def status_text(value):
    return {"success": "成功", "warning": "警告", "error": "失败", "partial": "部分成功", "failed": "失败"}.get(
        str(value), str(value)
    )


@app.template_filter("datetime_cn")
def datetime_cn(value):
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_CN_ZONE)
        return parsed.astimezone(_CN_ZONE).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _today():
    settings = store.load_settings()
    return datetime.now(_timezone(settings["timezone"])).date()


def _next_refresh_delay() -> float:
    settings = store.load_settings()
    now = datetime.now(_timezone(settings["timezone"]))
    hour_text, minute_text = settings.get("refresh_time", "06:30").split(":", 1)
    candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return max((candidate - now).total_seconds(), 1.0)


def _refresh_loop() -> None:
    while True:
        try:
            delay = _next_refresh_delay()
        except Exception:
            delay = 3600.0
        if _scheduler_wakeup.wait(delay):
            _scheduler_wakeup.clear()
            continue
        try:
            refresh_all()
        except Exception as exc:
            # The next run remains available; detailed provider failures are logged by refresh_all.
            _record_refresh_exception(exc)
            continue


def _parse_holdings(form) -> dict[str, float]:
    values: dict[str, float] = {}
    for asset in ALL_ASSETS:
        raw = str(form.get(f"holding_{asset}", "0")).strip().replace(",", "")
        try:
            value = float(raw or 0.0)
        except ValueError as exc:
            raise ValueError(f"{ASSET_META[asset]['label']} 的金额不是有效数字") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{ASSET_META[asset]['label']} 的金额必须是非负的有限数字")
        values[asset] = value
    if sum(values.values()) <= 0:
        raise ValueError("当前持仓和现金合计必须大于0")
    return values


def _parse_base_weights(form) -> dict[str, float]:
    percentages: dict[str, float] = {}
    for asset in ALL_ASSETS:
        raw = str(form.get(f"weight_{asset}", "")).strip().replace(",", "")
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{ASSET_META[asset]['label']} 的比例不是有效数字") from exc
        if not math.isfinite(value) or value < 0 or value > 100:
            raise ValueError(f"{ASSET_META[asset]['label']} 的比例必须在 0% 到 100% 之间")
        percentages[asset] = value
    total = sum(percentages.values())
    if not math.isclose(total, 100.0, abs_tol=0.01):
        raise ValueError(f"投资比例合计必须为 100%，当前为 {total:.1f}%")
    return {asset: value / total for asset, value in percentages.items()}


def _record_refresh_exception(exc: Exception) -> None:
    now = datetime.now(timezone.utc).isoformat()
    store.append_refresh_log(
        {
            "started_at": now,
            "finished_at": now,
            "status": "failed",
            "success_count": 0,
            "warning_count": 0,
            "error_count": 1,
            "items": [
                {
                    "name": "refresh_task",
                    "label": "刷新任务",
                    "status": "error",
                    "source": "application",
                    "message": str(exc),
                    "fallback": True,
                    "latest_date": None,
                    "rows": 0,
                }
            ],
        }
    )


def _input_values(values: dict[str, float]) -> dict[str, str]:
    return {asset: f"{float(values.get(asset, 0.0)):.2f}" for asset in ALL_ASSETS}


def _dashboard(calculation=None, values=None, error=None, signal_series=None):
    if values is None:
        values = store.load_portfolio_input()
    values = {asset: float(values.get(asset, 0.0)) for asset in ALL_ASSETS}
    today = _today()
    if calculation is None and sum(values[asset] for asset in ALL_ASSETS) > 0 and not error:
        try:
            calculation = calculate_plan(
                store,
                values,
                today,
                use_crossing_history=True,
                signal_data_mode="local_refreshed_history",
            )
        except Exception as exc:
            error = error or str(exc)
    return render_template(
        "dashboard.html",
        calculation=calculation,
        values=_input_values(values),
        overview=build_market_overview(store, today, signal_series),
        today=today.strftime("%Y-%m-%d"),
        error=error,
        asset_meta=ASSET_META,
        base_weights=effective_base_weights(store),
        housing=housing_schedule(store.load_settings().get("housing"), today),
    )


@app.get("/")
def dashboard():
    return _dashboard()


@app.post("/calculate")
def calculate():
    try:
        values = _parse_holdings(request.form)
        result = calculate_plan(
            store,
            values,
            _today(),
            use_crossing_history=True,
            signal_data_mode="local_refreshed_history",
        )
        store.save_portfolio_input(values)
        store.save_calculation(result)
        return _dashboard(result, values)
    except Exception as exc:
        values = {}
        for asset in ALL_ASSETS:
            try:
                value = float(str(request.form.get(f"holding_{asset}", 0)).replace(",", "") or 0)
                values[asset] = max(value, 0.0) if math.isfinite(value) else 0.0
            except ValueError:
                values[asset] = 0.0
        return _dashboard(values=values, error=str(exc)), 400


@app.get("/settings")
def settings():
    current = store.load_settings()
    logs = list(reversed(store.load_refresh_log()))[:20]
    return render_template(
        "settings.html",
        settings=current,
        logs=logs,
        asset_meta=ASSET_META,
        base_weights=effective_base_weights(store),
        housing=housing_schedule(current.get("housing"), _today()),
    )


@app.post("/settings")
def save_settings():
    raw_time = str(request.form.get("refresh_time", "")).strip()
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        flash("刷新时间格式必须是 00:00 到 23:59。", "error")
        return redirect(url_for("settings"))
    current = store.load_settings()
    current.update({"refresh_time": f"{hour:02d}:{minute:02d}", "timezone": current["timezone"]})
    store.save_settings(current)
    _scheduler_wakeup.set()
    flash(f"每日刷新时间已设置为 {hour:02d}:{minute:02d}（{current['timezone']}）。", "success")
    return redirect(url_for("settings"))


@app.post("/settings/allocation")
def save_allocation():
    try:
        base_weights = _parse_base_weights(request.form)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings"))
    current = store.load_settings()
    current["base_weights"] = base_weights
    store.save_settings(current)
    flash("投资比例已保存，并会用于后续目标仓位计算。", "success")
    return redirect(url_for("settings"))


@app.post("/settings/housing")
def save_housing():
    try:
        housing = validate_housing({
            "enabled": request.form.get("housing_enabled"),
            "purchase_month": request.form.get("purchase_month"),
            "target_amount": str(request.form.get("target_amount", "")).strip().replace(",", ""),
        })
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings"))
    current = store.load_settings()
    current["housing"] = housing
    store.save_settings(current)
    flash(
        "买房计划已启用，购房资金与普通策略国债分行计算，合计持有。" if housing["enabled"]
        else "买房计划已停用，计算页恢复普通投资计划。",
        "success",
    )
    return redirect(url_for("settings"))


@app.post("/refresh")
def refresh():
    try:
        result = refresh_all()
        flash(
            f"刷新完成：成功 {result['success_count']}，警告 {result['warning_count']}，失败 {result['error_count']}。",
            "success" if result["status"] == "success" else "warning",
        )
    except Exception as exc:
        _record_refresh_exception(exc)
        flash(f"刷新任务异常：{exc}", "error")
    return redirect(url_for("settings"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


threading.Thread(target=_refresh_loop, name="daily-market-refresh", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
