from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Callable

import pandas as pd
import requests


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = APP_ROOT / "data"
SEED_DIR = APP_ROOT / "seed" / "prices"

EASTMONEY_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

ASSET_FILES = {
    "csi300": "csi300.csv",
    "dividend_low_vol": "dividend_low_vol.csv",
    "star50": "star50.csv",
    "nasdaq100": "nasdaq100.csv",
    "gold": "gold.csv",
    "long_bond": "long_bond.csv",
    "cash": "cash.csv",
}

ASSET_META = {
    "csi300": {
        "label": "沪深300",
        "source": "东方财富 510300 复权日线；历史种子为沪深300长期代理",
    },
    "dividend_low_vol": {
        "label": "红利低波",
        "source": "东方财富 512890 复权日线；历史种子为红利低波指数序列",
    },
    "star50": {
        "label": "科创50",
        "source": "东方财富 588000 复权日线；正式历史不足时不补造代理",
    },
    "nasdaq100": {
        "label": "纳斯达克100",
        "source": "Yahoo Finance QQQ 复权价乘 FRED DEXCHUS，换算为人民币",
    },
    "gold": {
        "label": "黄金",
        "source": "Yahoo Finance GLD 复权价乘 FRED DEXCHUS，换算为人民币",
    },
    "long_bond": {
        "label": "境内长期国债",
        "source": "东方财富 511260 场内复权日线；历史不足时合并本地长期债券代理",
    },
    "cash": {
        "label": "人民币现金",
        "source": "本地现金收益代理，不作为风险资产均线信号",
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latest_date(series: pd.Series) -> str | None:
    if series.empty:
        return None
    return pd.Timestamp(series.index[-1]).strftime("%Y-%m-%d")


def _clean_series(values: pd.Series) -> pd.Series:
    series = pd.to_numeric(values, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index, errors="coerce").normalize()
    series = series[~series.index.isna()]
    return series[~series.index.duplicated(keep="last")].sort_index()


class MarketStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        configured = data_dir or Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
        self.data_dir = configured
        self.price_dir = self.data_dir / "prices"
        self.settings_path = self.data_dir / "settings.json"
        self.refresh_log_path = self.data_dir / "refresh_log.json"
        self.input_path = self.data_dir / "portfolio_input.json"
        self.calculation_path = self.data_dir / "last_calculation.json"

    def ensure_store(self) -> None:
        self.price_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for asset, filename in ASSET_FILES.items():
            target = self.price_dir / filename
            seed = SEED_DIR / filename
            if not target.exists() and seed.exists():
                target.write_bytes(seed.read_bytes())
        if not self.settings_path.exists():
            self.save_settings(
                {
                    "refresh_time": "06:30",
                    "timezone": os.environ.get("TZ", "Asia/Shanghai"),
                }
            )
        if not self.refresh_log_path.exists():
            self._atomic_json(self.refresh_log_path, [])

    def _atomic_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def _path_for(self, name: str) -> Path:
        return self.price_dir / ASSET_FILES.get(name, f"{name}.csv")

    def read_series(self, name: str) -> pd.Series:
        path = self._path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"missing local series: {path}")
        frame = pd.read_csv(path, parse_dates=["date"])
        if "close" not in frame.columns:
            raise ValueError(f"{path} has no close column")
        series = pd.to_numeric(frame["close"], errors="coerce")
        series.index = pd.to_datetime(frame["date"], errors="coerce")
        return _clean_series(series)

    def write_series(self, name: str, series: pd.Series) -> None:
        cleaned = _clean_series(series)
        if cleaned.empty:
            raise ValueError(f"cannot save empty series: {name}")
        path = self._path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        frame = cleaned.rename("close").rename_axis("date").reset_index()
        frame.to_csv(temp, index=False, date_format="%Y-%m-%d")
        os.replace(temp, path)

    def merge_and_write(self, name: str, incoming: pd.Series) -> pd.Series:
        try:
            existing = self.read_series(name)
        except (FileNotFoundError, ValueError):
            existing = pd.Series(dtype=float)
        incoming = _clean_series(incoming)
        if existing.empty:
            merged = incoming
        elif incoming.empty:
            merged = existing
        else:
            merged = pd.concat([existing, incoming])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self.write_series(name, merged)
        return merged

    def load_settings(self) -> dict[str, str]:
        self.ensure_store()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return {
            "refresh_time": str(raw.get("refresh_time", "06:30")),
            "timezone": str(raw.get("timezone", os.environ.get("TZ", "Asia/Shanghai"))),
        }

    def save_settings(self, settings: dict[str, str]) -> None:
        self._atomic_json(self.settings_path, settings)

    def load_refresh_log(self) -> list[dict[str, object]]:
        self.ensure_store()
        try:
            value = json.loads(self.refresh_log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def append_refresh_log(self, entry: dict[str, object]) -> None:
        entries = self.load_refresh_log()
        entries.append(entry)
        self._atomic_json(self.refresh_log_path, entries[-100:])

    def load_portfolio_input(self) -> dict[str, float]:
        try:
            value = json.loads(self.input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): float(raw) for key, raw in value.items()}

    def save_portfolio_input(self, values: dict[str, float]) -> None:
        self._atomic_json(self.input_path, values)

    def save_calculation(self, value: dict[str, object]) -> None:
        self._atomic_json(self.calculation_path, value)

    def load_calculation(self) -> dict[str, object] | None:
        try:
            value = json.loads(self.calculation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


def _market_id(code: str) -> int:
    return 1 if code.startswith(("5", "6")) else 0


def _request_json(session: requests.Session, url: str, params: dict[str, object]) -> dict:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = session.get(url, params=params, timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # network providers fail in several ways
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    raise RuntimeError(f"remote endpoint failed: {last_error}")


def fetch_eastmoney_daily(session: requests.Session, code: str) -> pd.Series:
    payload = _request_json(
        session,
        EASTMONEY_HISTORY_URL,
        {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",
            "fqt": "1",
            "beg": "0",
            "end": "20500101",
            "secid": f"{_market_id(code)}.{code}",
        },
    )
    rows = ((payload.get("data") or {}).get("klines")) or []
    if not rows:
        raise RuntimeError(f"Eastmoney returned no daily data for {code}")
    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for raw_row in rows:
        row = str(raw_row).split(",")
        if len(row) < 3:
            continue
        date = pd.to_datetime(row[0], errors="coerce")
        close = pd.to_numeric(row[2], errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date).normalize())
            closes.append(float(close))
    if not closes:
        raise RuntimeError(f"Eastmoney returned no valid closes for {code}")
    return _clean_series(pd.Series(closes, index=dates))


def fetch_yahoo_adjusted_close(
    session: requests.Session, symbol: str, start: str = "2005-01-01"
) -> pd.Series:
    start_timestamp = int(pd.Timestamp(start, tz="UTC").timestamp())
    end_timestamp = int((pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=2)).timestamp())
    payload = _request_json(
        session,
        YAHOO_CHART_URL.format(symbol=symbol),
        {
            "period1": start_timestamp,
            "period2": end_timestamp,
            "interval": "1d",
            "events": "history",
        },
    )
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        raise RuntimeError(f"Yahoo returned no daily data for {symbol}")
    timestamps = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_localize(None).normalize()
    indicators = result.get("indicators") or {}
    adjusted = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    if adjusted is None:
        adjusted = (indicators.get("quote") or [{}])[0].get("close")
    if adjusted is None:
        raise RuntimeError(f"Yahoo returned no close data for {symbol}")
    return _clean_series(pd.Series(adjusted, index=timestamps))


def fetch_fred_series(
    session: requests.Session, series_id: str, start: str = "2005-01-01"
) -> pd.Series:
    response = session.get(
        FRED_CSV_URL,
        params={
            "id": series_id,
            "cosd": start,
            "coed": (datetime.now().date() + timedelta(days=1)).isoformat(),
        },
        timeout=20,
    )
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text))
    date_column = "observation_date" if "observation_date" in frame else "DATE"
    if series_id not in frame.columns:
        raise RuntimeError(f"FRED response has no {series_id} column")
    values = pd.to_numeric(frame[series_id], errors="coerce")
    series = pd.Series(values.to_numpy(), index=pd.to_datetime(frame[date_column], errors="coerce"))
    series = _clean_series(series)
    if series.empty:
        raise RuntimeError(f"FRED returned no observations for {series_id}")
    return series


def _status(
    name: str,
    state: str,
    source: str,
    message: str,
    series: pd.Series | None = None,
    fallback: bool = False,
    label: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "label": label or ASSET_META.get(name, {}).get("label", name),
        "status": state,
        "source": source,
        "message": message,
        "fallback": fallback,
        "latest_date": _latest_date(series) if series is not None else None,
        "rows": int(len(series)) if series is not None else 0,
    }


def _update_remote_series(
    store: MarketStore,
    session: requests.Session,
    name: str,
    label: str,
    source: str,
    fetch: Callable[[], pd.Series],
) -> dict[str, object]:
    try:
        incoming = fetch()
        merged = store.merge_and_write(name, incoming)
        return _status(name, "success", source, "远程数据已合并到本地", merged, label=label)
    except Exception as exc:
        try:
            cached = store.read_series(name)
            return _status(
                name,
                "warning",
                "local_cache",
                f"远程刷新失败，沿用本地数据：{exc}",
                cached,
                fallback=True,
                label=label,
            )
        except Exception:
            return _status(name, "error", source, str(exc), label=label)


def _combine_cny_series(
    store: MarketStore,
    name: str,
    usd_name: str,
    dependency_states: list[dict[str, object]],
) -> dict[str, object]:
    try:
        usd = store.read_series(usd_name)
        fx = store.read_series("usd_cny")
        frame = pd.concat({"usd": usd, "fx": fx}, axis=1).sort_index().ffill().dropna()
        frame = frame[(frame["usd"] > 0) & (frame["fx"] > 0)]
        cny = frame["usd"] * frame["fx"]
        merged = store.merge_and_write(name, cny)
        dependency_warning = any(item["status"] != "success" for item in dependency_states)
        state = "warning" if dependency_warning else "success"
        message = "美元价格与 USD/CNY 已合并为人民币序列"
        if dependency_warning:
            message += "；部分远程依赖使用了本地缓存"
        return _status(name, state, ASSET_META[name]["source"], message, merged, dependency_warning)
    except Exception as exc:
        try:
            cached = store.read_series(name)
            return _status(
                name,
                "warning",
                "local_cache",
                f"人民币序列刷新失败，沿用本地数据：{exc}",
                cached,
                fallback=True,
            )
        except Exception:
            return _status(name, "error", ASSET_META[name]["source"], str(exc))


def refresh_all(data_dir: Path | None = None) -> dict[str, object]:
    store = MarketStore(data_dir)
    store.ensure_store()
    started = _utc_now()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "long-term-strategy-local-app/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    statuses: list[dict[str, object]] = []

    domestic = [
        ("csi300", "510300"),
        ("dividend_low_vol", "512890"),
        ("star50", "588000"),
        ("long_bond", "511260"),
    ]
    for asset, code in domestic:
        statuses.append(
            _update_remote_series(
                store,
                session,
                asset,
                ASSET_META[asset]["label"],
                ASSET_META[asset]["source"],
                lambda code=code: fetch_eastmoney_daily(session, code),
            )
        )

    fx_status = _update_remote_series(
        store,
        session,
        "usd_cny",
        "USD/CNY",
        "FRED DEXCHUS",
        lambda: fetch_fred_series(session, "DEXCHUS"),
    )
    qqq_status = _update_remote_series(
        store,
        session,
        "qqq_usd",
        "QQQ美元价格",
        "Yahoo Finance QQQ",
        lambda: fetch_yahoo_adjusted_close(session, "QQQ"),
    )
    gld_status = _update_remote_series(
        store,
        session,
        "gld_usd",
        "GLD美元价格",
        "Yahoo Finance GLD",
        lambda: fetch_yahoo_adjusted_close(session, "GLD"),
    )
    statuses.extend([fx_status, qqq_status, gld_status])
    statuses.append(_combine_cny_series(store, "nasdaq100", "qqq_usd", [fx_status, qqq_status]))
    statuses.append(_combine_cny_series(store, "gold", "gld_usd", [fx_status, gld_status]))

    try:
        cash = store.read_series("cash")
        statuses.append(
            _status(
                "cash",
                "success",
                ASSET_META["cash"]["source"],
                "现金收益代理保留本地序列",
                cash,
            )
        )
    except Exception as exc:
        statuses.append(_status("cash", "error", ASSET_META["cash"]["source"], str(exc)))

    errors = [item for item in statuses if item["status"] == "error"]
    warnings = [item for item in statuses if item["status"] == "warning"]
    overall = "failed" if errors and len(errors) == len(statuses) else "partial" if errors or warnings else "success"
    result: dict[str, object] = {
        "started_at": started,
        "finished_at": _utc_now(),
        "status": overall,
        "success_count": sum(item["status"] == "success" for item in statuses),
        "warning_count": len(warnings),
        "error_count": len(errors),
        "items": statuses,
    }
    store.append_refresh_log(result)
    return result
