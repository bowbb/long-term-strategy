from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Callable
from zipfile import ZipFile

import pandas as pd
import requests


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = APP_ROOT / "data"

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ECB_HISTORICAL_ZIP_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
SSE_DAYK_URL = "http://yunhq.sse.com.cn:32041/v1/sh1/dayk/{code}"
CSINDEX_HISTORY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
NASDAQ_HISTORICAL_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
GLD_HISTORICAL_ARCHIVE_URL = (
    "https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en&product=gld"
)
CN_TIMEZONE = "Asia/Shanghai"
REFRESH_RETRY_DELAY_SECONDS = 60.0
MAX_REFRESH_RETRIES = 5
DATA_SCHEMA_VERSION = 3

ASSET_FILES = {
    "dividend_low_vol": "dividend_low_vol.csv",
    "nasdaq100": "nasdaq100.csv",
    "gold": "gold.csv",
    "long_bond": "long_bond.csv",
}

ASSET_META = {
    "dividend_low_vol": {
        "label": "红利低波",
        "source": "中证指数官方 H20269 红利低波全收益指数",
        "source_url": f"{CSINDEX_HISTORY_URL}?indexCode=H20269&startDate=20050101",
        "instrument": "H20269",
    },
    "nasdaq100": {
        "label": "纳斯达克100",
        "source": "Nasdaq 官方 QQQ 日线收盘价乘美联储 H.10 USD/CNY",
        "source_url": NASDAQ_HISTORICAL_URL.format(symbol="QQQ"),
        "instrument": "QQQ",
    },
    "gold": {
        "label": "黄金",
        "source": "State Street 官方 GLD 历史收盘价乘美联储 H.10 USD/CNY",
        "source_url": GLD_HISTORICAL_ARCHIVE_URL,
        "instrument": "GLD",
    },
    "long_bond": {
        "label": "境内长期国债",
        "source": "上交所官方日线 511260（原始 ETF 收盘价）",
        "source_url": SSE_DAYK_URL.format(code="511260"),
        "instrument": "511260",
    },
    "cash": {
        "label": "人民币现金",
        "source": "手动输入的人民币现金金额，不使用行情序列",
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


def _validate_official_series(name: str, values: pd.Series) -> pd.Series:
    series = _clean_series(values)
    if series.empty:
        raise RuntimeError(f"官方数据为空：{name}")
    if (series <= 0).any():
        raise RuntimeError(f"官方数据包含非正价格：{name}")
    if len(series) > 1:
        returns = series.pct_change().dropna()
        if not returns.empty and float(returns.abs().max()) > 10.0:
            raise RuntimeError(f"官方数据存在异常尺度跳变：{name}")
    latest = pd.Timestamp(series.index[-1]).normalize()
    today = pd.Timestamp.now(tz=CN_TIMEZONE).tz_localize(None).normalize()
    if latest < today - pd.Timedelta(days=14):
        raise RuntimeError(f"官方数据过旧：{name} 最后日期为 {latest.date()}")
    return series


def _series_digest(series: pd.Series | None) -> str | None:
    if series is None:
        return None
    cleaned = _clean_series(series)
    if cleaned.empty:
        return None
    payload = "\n".join(
        f"{pd.Timestamp(date).strftime('%Y-%m-%d')},{float(value):.12g}"
        for date, value in cleaned.items()
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _completed_daily_series(series: pd.Series) -> pd.Series:
    """Keep the latest completed local trading day, never today's unfinished bar."""
    today_in_cn = pd.Timestamp.now(tz=CN_TIMEZONE).normalize().tz_localize(None)
    cutoff = today_in_cn - pd.Timedelta(days=1)
    return _clean_series(series[series.index <= cutoff])


class MarketStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        configured = data_dir or Path(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
        self.data_dir = configured
        self.price_dir = self.data_dir / "prices"
        self.legacy_price_dir = self.data_dir / "legacy_prices"
        self.schema_path = self.data_dir / "source_schema.json"
        self.settings_path = self.data_dir / "settings.json"
        self.refresh_log_path = self.data_dir / "refresh_log.json"
        self.input_path = self.data_dir / "portfolio_input.json"
        self.calculation_path = self.data_dir / "last_calculation.json"

    def ensure_store(self) -> None:
        self.price_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_official_store()
        active_files = set(ASSET_FILES.values()) | {"usd_cny.csv", "qqq_usd.csv", "gld_usd.csv"}
        for cached_path in self.price_dir.glob("*.csv"):
            if cached_path.name not in active_files:
                cached_path.unlink()
        if not self.settings_path.exists():
            self.save_settings(
                {
                    "refresh_time": "06:30",
                    "timezone": os.environ.get("TZ", "Asia/Shanghai"),
                }
            )
        if not self.refresh_log_path.exists():
            self._atomic_json(self.refresh_log_path, [])
        self._migrate_runtime_state()

    def _prepare_official_store(self) -> None:
        """Quarantine stale source data before changing the official data policy."""
        try:
            state = json.loads(self.schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if isinstance(state, dict) and int(state.get("version", 0)) >= DATA_SCHEMA_VERSION:
            return

        previous_version = int(state.get("version", 0)) if isinstance(state, dict) else 0
        if previous_version < 2:
            legacy_files = list(self.price_dir.glob("*.csv"))
        else:
            dividend_path = self.price_dir / ASSET_FILES["dividend_low_vol"]
            legacy_files = [dividend_path] if dividend_path.exists() else []
        if legacy_files:
            target_dir = self.legacy_price_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target_dir.mkdir(parents=True, exist_ok=True)
            for path in legacy_files:
                shutil.move(str(path), str(target_dir / path.name))
        if self.calculation_path.exists():
            state_dir = self.data_dir / "legacy_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.calculation_path), str(state_dir / "last_calculation.json"))
        self._atomic_json(
            self.schema_path,
            {
                "version": DATA_SCHEMA_VERSION,
                "source_policy": "official_only_csindex_h20269",
                "requires_refresh": True,
                "migrated_at": _utc_now(),
            },
        )

    def _atomic_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def _migrate_runtime_state(self) -> None:
        active_assets = set(ASSET_META)
        if self.input_path.exists():
            try:
                raw_input = json.loads(self.input_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_input = {}
            if isinstance(raw_input, dict):
                filtered_input = {key: value for key, value in raw_input.items() if key in active_assets}
                if filtered_input != raw_input:
                    self._atomic_json(self.input_path, filtered_input)

        if self.calculation_path.exists():
            try:
                calculation = json.loads(self.calculation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                calculation = None
            rows = calculation.get("rows", []) if isinstance(calculation, dict) else []
            if any(isinstance(row, dict) and row.get("asset") not in active_assets for row in rows):
                self.calculation_path.unlink()

        allowed_refresh_names = set(ASSET_FILES) | {"usd_cny", "qqq_usd", "gld_usd", "refresh_task"}
        try:
            refresh_entries = json.loads(self.refresh_log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            refresh_entries = []
        changed = False
        if isinstance(refresh_entries, list):
            for entry in refresh_entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
                    continue
                items = [
                    item
                    for item in entry["items"]
                    if isinstance(item, dict) and item.get("name") in allowed_refresh_names
                ]
                if items != entry["items"]:
                    entry["items"] = items
                    entry["success_count"] = sum(item.get("status") == "success" for item in items)
                    entry["warning_count"] = sum(item.get("status") == "warning" for item in items)
                    entry["error_count"] = sum(item.get("status") == "error" for item in items)
                    changed = True
            if changed:
                self._atomic_json(self.refresh_log_path, refresh_entries)

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

    def replace_series(self, name: str, series: pd.Series) -> pd.Series:
        """Replace the full local history with a validated official series."""
        cleaned = _validate_official_series(name, series)
        self.write_series(name, cleaned)
        return cleaned

    def merge_and_write(self, name: str, incoming: pd.Series) -> pd.Series:
        """Append compatible data without silently changing its price scale.

        Official refreshes use :meth:`replace_series`; this method remains only
        for callers that intentionally append a same-scale local series.
        """
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
            overlap = existing.index.intersection(incoming.index)
            if len(overlap) > 0:
                reference = overlap[-min(20, len(overlap)) :]
                ratios = (existing.loc[reference] / incoming.loc[reference]).replace(
                    [float("inf"), float("-inf")], pd.NA
                ).dropna()
                ratios = ratios[ratios > 0]
                if not ratios.empty:
                    scale = float(ratios.median())
                    if scale < 0.8 or scale > 1.25:
                        raise RuntimeError(
                            f"拒绝拼接不同价格尺度的数据：{name} 中位比例 {scale:.6g}"
                        )
            incoming = incoming[incoming.index > existing.index[-1]]
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
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, object]] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            raw_items = entry.get("items", [])
            if not isinstance(raw_items, list):
                raw_items = []
            allowed_names = set(ASSET_FILES) | {"usd_cny", "qqq_usd", "gld_usd", "refresh_task"}
            items = [item for item in raw_items if isinstance(item, dict) and item.get("name") in allowed_names]
            entry = dict(entry)
            entry["items"] = items
            entry["success_count"] = sum(item.get("status") == "success" for item in items)
            entry["warning_count"] = sum(item.get("status") == "warning" for item in items)
            entry["error_count"] = sum(item.get("status") == "error" for item in items)
            normalized.append(entry)
        return normalized

    def append_refresh_log(self, entry: dict[str, object]) -> None:
        entries = self.load_refresh_log()
        entries.append(entry)
        self._atomic_json(self.refresh_log_path, entries[-100:])

    def load_portfolio_input(self) -> dict[str, float]:
        try:
            value = json.loads(self.input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): float(raw) for key, raw in value.items() if key in ASSET_META}

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


def _request_json(
    session: requests.Session,
    url: str,
    params: dict[str, object],
    headers: dict[str, str] | None = None,
) -> dict:
    try:
        response = session.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # network providers fail in several ways
        raise RuntimeError(f"remote endpoint failed: {exc}") from exc


def fetch_sse_daily(
    session: requests.Session, code: str, start: str = "2005-01-01"
) -> tuple[pd.Series, str]:
    """Read official Shanghai Stock Exchange daily ETF closes."""
    payload = _request_json(
        session,
        SSE_DAYK_URL.format(code=code),
        {"begin": "-10000", "end": "-1", "period": "day"},
        headers={"Referer": "https://www.sse.com.cn/", "Accept": "application/json"},
    )
    rows = payload.get("kline") or []
    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        date = pd.to_datetime(str(row[0]), format="%Y%m%d", errors="coerce")
        close = pd.to_numeric(row[4], errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date).normalize())
            closes.append(float(close))
    series = _completed_daily_series(pd.Series(closes, index=dates))
    start_date = pd.Timestamp(start).normalize()
    series = series.loc[series.index >= start_date]
    return _validate_official_series(f"SSE {code}", series), f"SSE 官方日线 {code}"


def fetch_csindex_history(
    session: requests.Session,
    index_code: str,
    start: str = "2005-01-01",
) -> tuple[pd.Series, str]:
    """Read an official CSI daily total-return index history."""
    start_date = pd.Timestamp(start).normalize()
    end_date = (
        pd.Timestamp.now(tz=CN_TIMEZONE).date() + pd.Timedelta(days=1)
    ).strftime("%Y%m%d")
    try:
        response = session.get(
            CSINDEX_HISTORY_URL,
            params={
                "indexCode": index_code,
                "startDate": start_date.strftime("%Y%m%d"),
                "endDate": end_date,
            },
            headers={
                "User-Agent": "long-term-strategy-local-app/1.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.csindex.com.cn/",
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"CSIndex {index_code} endpoint failed: {exc}") from exc

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"CSIndex {index_code} response has no data list")

    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = pd.to_datetime(
            str(row.get("tradeDate") or ""),
            format="%Y%m%d",
            errors="coerce",
        )
        close = pd.to_numeric(row.get("close"), errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date).normalize())
            closes.append(float(close))

    series = _completed_daily_series(pd.Series(closes, index=dates, name=index_code))
    series = series.loc[series.index >= start_date]
    return _validate_official_series(
        f"CSIndex {index_code}", series
    ), f"中证指数官方 {index_code} 全收益指数"


def fetch_nasdaq_historical(
    session: requests.Session,
    symbol: str,
    asset_class: str,
    start: str = "2005-01-01",
) -> tuple[pd.Series, str]:
    """Read official Nasdaq historical daily closes with pagination."""
    start_date = pd.Timestamp(start).date().isoformat()
    end_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    rows: list[dict[str, object]] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        payload = _request_json(
            session,
            NASDAQ_HISTORICAL_URL.format(symbol=symbol),
            {
                "assetclass": asset_class,
                "fromdate": start_date,
                "todate": end_date,
                "limit": 5000,
                "offset": offset,
            },
            headers={
                "User-Agent": "long-term-strategy-local-app/1.0",
                "Accept": "application/json",
                "Origin": "https://www.nasdaq.com",
                "Referer": f"https://www.nasdaq.com/market-activity/{asset_class}/{symbol.lower()}/historical",
            },
        )
        data = payload.get("data") or {}
        table = data.get("tradesTable") if isinstance(data, dict) else None
        page = table.get("rows") if isinstance(table, dict) else None
        if not isinstance(page, list) or not page:
            break
        rows.extend(item for item in page if isinstance(item, dict))
        raw_total = data.get("totalRecords") if isinstance(data, dict) else None
        try:
            total = int(raw_total) if raw_total is not None else offset + len(page)
        except (TypeError, ValueError):
            total = offset + len(page)
        offset += len(page)
        if len(page) < 5000:
            break

    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for row in rows:
        date = pd.to_datetime(row.get("date"), format="%m/%d/%Y", errors="coerce")
        raw_close = str(row.get("close", "")).replace(",", "")
        close = pd.to_numeric(raw_close, errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date).normalize())
            closes.append(float(close))
    series = _completed_daily_series(pd.Series(closes, index=dates))
    return _validate_official_series(f"Nasdaq {symbol}", series), f"Nasdaq 官方 {symbol} 日线"


def fetch_gld_historical(session: requests.Session, start: str = "2005-01-01") -> tuple[pd.Series, str]:
    """Read State Street's official GLD historical archive workbook."""
    response = session.get(
        GLD_HISTORICAL_ARCHIVE_URL,
        headers={"User-Agent": "long-term-strategy-local-app/1.0"},
        timeout=60,
    )
    response.raise_for_status()
    try:
        frame = pd.read_excel(BytesIO(response.content), sheet_name="US GLD Historical Archive")
    except Exception as exc:
        raise RuntimeError(f"GLD 官方历史归档无法读取：{exc}") from exc
    if "Date" not in frame or "Closing Price" not in frame:
        raise RuntimeError("GLD 官方历史归档缺少 Date/Closing Price 列")
    dates = pd.to_datetime(frame["Date"], errors="coerce")
    closes = pd.to_numeric(frame["Closing Price"], errors="coerce")
    series = _completed_daily_series(pd.Series(closes.to_numpy(), index=dates))
    series = series.loc[series.index >= pd.Timestamp(start).normalize()]
    return _validate_official_series("GLD", series), "State Street 官方 GLD 历史归档"


def fetch_domestic_daily(
    session: requests.Session, code: str
) -> tuple[pd.Series, str]:
    return fetch_sse_daily(session, code)


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


def fetch_ecb_usd_cny(session: requests.Session) -> pd.Series:
    response = session.get(ECB_HISTORICAL_ZIP_URL, timeout=20)
    response.raise_for_status()
    with ZipFile(BytesIO(response.content)) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("ECB archive has no CSV data")
        with archive.open(csv_names[0]) as csv_file:
            frame = pd.read_csv(csv_file)

    columns = {str(column).upper(): column for column in frame.columns}
    required = {key: columns.get(key) for key in ("DATE", "USD", "CNY")}
    if any(value is None for value in required.values()):
        raise RuntimeError("ECB CSV has no Date/USD/CNY columns")
    dates = pd.to_datetime(frame[required["DATE"]], errors="coerce")
    usd = pd.to_numeric(frame[required["USD"]], errors="coerce")
    cny = pd.to_numeric(frame[required["CNY"]], errors="coerce")
    series = _clean_series(pd.Series(cny.to_numpy() / usd.to_numpy(), index=dates))
    series = series[(series > 0) & series.notna()]
    series = _completed_daily_series(series)
    if series.empty:
        raise RuntimeError("ECB returned no valid USD/CNY observations")
    return series


def fetch_fx_usd_cny(
    session: requests.Session, start: str = "2005-01-01"
) -> tuple[pd.Series, str]:
    """Fetch USD/CNY from official central-bank sources only."""
    providers: list[tuple[str, Callable[[], pd.Series]]] = [
        ("FRED DEXCHUS", lambda: fetch_fred_series(session, "DEXCHUS", start)),
        ("ECB eurofxref-hist 官方参考汇率", lambda: fetch_ecb_usd_cny(session)),
    ]
    failures: list[str] = []
    for provider, fetch in providers:
        try:
            series = _completed_daily_series(fetch())
            if not series.empty:
                return series, provider
        except Exception as exc:
            failures.append(f"{provider}: {exc}")
    raise RuntimeError(f"所有 USD/CNY 数据源均失败：{'；'.join(failures)}")


def _status(
    name: str,
    state: str,
    source: str,
    message: str,
    series: pd.Series | None = None,
    fallback: bool = False,
    label: str | None = None,
    source_url: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "label": label or ASSET_META.get(name, {}).get("label", name),
        "status": state,
        "source": source,
        "message": message,
        "fallback": fallback,
        "source_url": source_url or ASSET_META.get(name, {}).get("source_url"),
        "checked_at": _utc_now(),
        "latest_date": _latest_date(series) if series is not None else None,
        "latest_value": float(series.iloc[-1]) if series is not None and not series.empty else None,
        "rows": int(len(series)) if series is not None else 0,
        "series_sha256": _series_digest(series),
    }


def _update_remote_series(
    store: MarketStore,
    session: requests.Session,
    name: str,
    label: str,
    source: str,
    source_url: str,
    fetch: Callable[[], pd.Series | tuple[pd.Series, str]],
    replace: bool = True,
) -> dict[str, object]:
    try:
        fetched = fetch()
        provider = source
        if isinstance(fetched, tuple):
            incoming, provider = fetched
        else:
            incoming = fetched
        incoming = _validate_official_series(name, incoming)
        stored = store.replace_series(name, incoming) if replace else store.merge_and_write(name, incoming)
        return _status(
            name,
            "success",
            provider,
            f"{provider} 数据已替换本地历史" if replace else f"{provider} 数据已追加到本地",
            stored,
            label=label,
            source_url=source_url,
        )
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
                source_url=source_url,
            )
        except Exception:
            return _status(name, "error", source, str(exc), label=label, source_url=source_url)


def _refresh_remote_tasks(
    store: MarketStore,
    session: requests.Session,
    tasks: list[
        tuple[str, str, str, str, Callable[[], pd.Series | tuple[pd.Series, str]], bool]
    ],
) -> list[dict[str, object]]:
    """Run remote tasks serially, retrying only failed tasks between rounds."""
    statuses: dict[str, dict[str, object]] = {}
    for name, label, source, source_url, fetch, replace in tasks:
        statuses[name] = _update_remote_series(
            store, session, name, label, source, source_url, fetch, replace
        )

    for retry_number in range(1, MAX_REFRESH_RETRIES + 1):
        failed_tasks = [task for task in tasks if statuses[task[0]]["status"] != "success"]
        if not failed_tasks:
            break
        time.sleep(REFRESH_RETRY_DELAY_SECONDS)
        for name, label, source, source_url, fetch, replace in failed_tasks:
            retry_status = _update_remote_series(
                store, session, name, label, source, source_url, fetch, replace
            )
            if retry_status["status"] != "success":
                retry_status = dict(retry_status)
                retry_status["message"] = (
                    f"{retry_status['message']}；已完成第 {retry_number}/{MAX_REFRESH_RETRIES} 次重试"
                )
            statuses[name] = retry_status

    return [statuses[name] for name, _, _, _, _, _ in tasks]


def _combine_cny_series(
    store: MarketStore,
    name: str,
    usd_name: str,
    dependency_states: list[dict[str, object]],
) -> dict[str, object]:
    try:
        dependency_warning = any(item["status"] != "success" for item in dependency_states)
        fx_source = next(
            (str(item["source"]) for item in dependency_states if item["name"] == "usd_cny"),
            "本地 USD/CNY",
        )
        if dependency_warning:
            cached = store.read_series(name)
            return _status(
                name,
                "warning",
                "local_cache",
                f"美元价格或 USD/CNY 刷新失败，沿用上一份人民币序列：{fx_source}",
                cached,
                fallback=True,
                source_url=ASSET_META[name].get("source_url"),
            )

        usd = store.read_series(usd_name)
        fx = store.read_series("usd_cny")
        # Keep only ETF trading dates; forward-fill FX inside that date index.
        frame = usd.rename("usd").to_frame().join(fx.rename("fx"), how="left")
        frame["fx"] = frame["fx"].ffill()
        frame = frame.dropna()
        frame = frame[(frame["usd"] > 0) & (frame["fx"] > 0)]
        cny = frame["usd"] * frame["fx"]
        merged = store.replace_series(name, cny)
        message = f"美元价格与 USD/CNY（{fx_source}）已合并为人民币序列"
        return _status(
            name,
            "success",
            ASSET_META[name]["source"],
            message,
            merged,
            False,
            source_url=ASSET_META[name].get("source_url"),
        )
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
                source_url=ASSET_META[name].get("source_url"),
            )
        except Exception:
            return _status(
                name,
                "error",
                ASSET_META[name]["source"],
                str(exc),
                source_url=ASSET_META[name].get("source_url"),
            )


_REFRESH_LOCK = threading.Lock()


def refresh_all(data_dir: Path | None = None) -> dict[str, object]:
    with _REFRESH_LOCK:
        return _refresh_all(data_dir)


def _refresh_all(data_dir: Path | None = None) -> dict[str, object]:
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

    tasks: list[
        tuple[str, str, str, str, Callable[[], pd.Series | tuple[pd.Series, str]], bool]
    ] = []
    tasks.append(
        (
            "dividend_low_vol",
            ASSET_META["dividend_low_vol"]["label"],
            ASSET_META["dividend_low_vol"]["source"],
            ASSET_META["dividend_low_vol"]["source_url"],
            lambda: fetch_csindex_history(session, "H20269"),
            True,
        )
    )
    tasks.append(
        (
            "long_bond",
            ASSET_META["long_bond"]["label"],
            ASSET_META["long_bond"]["source"],
            ASSET_META["long_bond"]["source_url"],
            lambda: fetch_domestic_daily(session, "511260"),
            True,
        )
    )

    tasks.extend(
        [
            (
                "usd_cny",
                "USD/CNY",
                "FRED DEXCHUS -> ECB 官方参考汇率",
                f"{FRED_CSV_URL}?id=DEXCHUS",
                lambda: fetch_fx_usd_cny(session),
                True,
            ),
            (
                "qqq_usd",
                "QQQ美元价格",
                "Nasdaq 官方 QQQ 日线",
                NASDAQ_HISTORICAL_URL.format(symbol="QQQ"),
                lambda: fetch_nasdaq_historical(session, "QQQ", "etf"),
                True,
            ),
            (
                "gld_usd",
                "GLD美元价格",
                "State Street 官方 GLD 历史归档",
                GLD_HISTORICAL_ARCHIVE_URL,
                lambda: fetch_gld_historical(session),
                True,
            ),
        ]
    )
    statuses.extend(_refresh_remote_tasks(store, session, tasks))
    status_by_name = {item["name"]: item for item in statuses}
    fx_status = status_by_name["usd_cny"]
    qqq_status = status_by_name["qqq_usd"]
    gld_status = status_by_name["gld_usd"]
    statuses.append(_combine_cny_series(store, "nasdaq100", "qqq_usd", [fx_status, qqq_status]))
    statuses.append(_combine_cny_series(store, "gold", "gld_usd", [fx_status, gld_status]))

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
    if statuses and all(item["status"] == "success" for item in statuses):
        try:
            state = json.loads(store.schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if isinstance(state, dict):
            state.update({"requires_refresh": False, "last_official_refresh": result["finished_at"]})
            store._atomic_json(store.schema_path, state)
    store.append_refresh_log(result)
    return result
