from __future__ import annotations

import json
import os
import re
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
SEED_DIR = APP_ROOT / "seed" / "prices"

EASTMONEY_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketDataService.getKLineData"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRANKFURTER_TIMESERIES_URL = "https://api.frankfurter.dev/v1/{start}.."
ECB_HISTORICAL_ZIP_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
CN_TIMEZONE = "Asia/Shanghai"
REFRESH_RETRY_DELAY_SECONDS = 60.0
MAX_REFRESH_RETRIES = 5

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
        "source": "腾讯前复权 510300；Yahoo/新浪/东方财富备用，历史种子为沪深300长期代理",
    },
    "dividend_low_vol": {
        "label": "红利低波",
        "source": "腾讯前复权 512890；Yahoo/新浪/东方财富备用，历史种子为红利低波指数序列",
    },
    "star50": {
        "label": "科创50",
        "source": "腾讯前复权 588000；Yahoo/新浪/东方财富备用，正式历史不足时不补造代理",
    },
    "nasdaq100": {
        "label": "纳斯达克100",
        "source": "Yahoo Finance QQQ 复权价乘多源 USD/CNY，换算为人民币",
    },
    "gold": {
        "label": "黄金",
        "source": "Yahoo Finance GLD 复权价乘多源 USD/CNY，换算为人民币",
    },
    "long_bond": {
        "label": "境内长期国债",
        "source": "腾讯前复权 511260；Yahoo/新浪/东方财富备用，历史不足时合并本地长期债券代理",
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
            # ETF feeds and archived index proxies can use different price scales.
            # Align the incoming level to the recent overlap, then append only new dates.
            overlap = existing.index.intersection(incoming.index)
            if len(overlap) > 0:
                reference = overlap[-min(20, len(overlap)) :]
                ratios = (existing.loc[reference] / incoming.loc[reference]).replace(
                    [float("inf"), float("-inf")], pd.NA
                ).dropna()
                ratios = ratios[ratios > 0]
                if not ratios.empty:
                    incoming = incoming * float(ratios.median())
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
            # Cash is a local input series and has never been a remote refresh task.
            raw_items = entry.get("items", [])
            if not isinstance(raw_items, list):
                raw_items = []
            items = [
                item
                for item in raw_items
                if isinstance(item, dict) and item.get("name") != "cash"
            ]
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


def _cn_market_prefix(code: str) -> str:
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def _parse_daily_rows(rows: list[object], code: str, source: str) -> pd.Series:
    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for raw_row in rows:
        if isinstance(raw_row, dict):
            date_value = raw_row.get("date") or raw_row.get("day")
            close_value = raw_row.get("close")
        else:
            row = list(raw_row) if isinstance(raw_row, (list, tuple)) else str(raw_row).split(",")
            if len(row) < 3:
                continue
            date_value = row[0]
            close_value = row[2]
        date = pd.to_datetime(date_value, errors="coerce")
        close = pd.to_numeric(close_value, errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date).normalize())
            closes.append(float(close))
    series = _clean_series(pd.Series(closes, index=dates))
    if series.empty:
        raise RuntimeError(f"{source} returned no valid daily data for {code}")
    return series


def fetch_tencent_adjusted_close(session: requests.Session, code: str) -> pd.Series:
    symbol = f"{_cn_market_prefix(code)}{code}"
    payload = _request_json(
        session,
        TENCENT_KLINE_URL,
        {"param": f"{symbol},day,,,10000,qfq"},
        headers={"Referer": "https://gu.qq.com/"},
    )
    quote = ((payload.get("data") or {}).get(symbol)) or {}
    rows = quote.get("qfqday") or quote.get("day") or []
    return _parse_daily_rows(rows, code, "腾讯财经")


def _decode_json_or_jsonp(response: requests.Response) -> object:
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        match = re.search(r"(\[.*\]|\{.*\})", response.text, flags=re.DOTALL)
        if not match:
            raise RuntimeError("response is neither JSON nor JSONP")
        return json.loads(match.group(1))


def fetch_sina_daily(session: requests.Session, code: str) -> pd.Series:
    payload = _decode_json_or_jsonp(
        session.get(
            SINA_KLINE_URL,
            params={
                "symbol": f"{_cn_market_prefix(code)}{code}",
                "scale": "240",
                "ma": "no",
                "datalen": "10000",
            },
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=20,
        )
    )
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("klines") or []
    else:
        rows = payload
    if not isinstance(rows, list):
        raise RuntimeError(f"新浪财经 returned an unexpected payload for {code}")
    return _parse_daily_rows(rows, code, "新浪财经")


def fetch_domestic_daily(
    session: requests.Session, code: str
) -> tuple[pd.Series, str]:
    providers: list[tuple[str, Callable[[], pd.Series]]] = [
        (
            "腾讯财经前复权",
            lambda: fetch_tencent_adjusted_close(session, code),
        ),
        (
            "Yahoo Finance ETF 复权价",
            lambda: fetch_yahoo_adjusted_close(session, f"{code}.SS"),
        ),
        (
            "新浪财经日线",
            lambda: fetch_sina_daily(session, code),
        ),
        (
            "东方财富复权日线",
            lambda: fetch_eastmoney_daily(session, code),
        ),
    ]
    failures: list[str] = []
    for provider, fetch in providers:
        try:
            series = fetch()
            series = _completed_daily_series(series)
            if not series.empty:
                return series, provider
        except Exception as exc:
            failures.append(f"{provider}: {exc}")
    detail = "；".join(failures)
    raise RuntimeError(f"所有境内行情源均失败（{code}）：{detail}")


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


def fetch_frankfurter_usd_cny(
    session: requests.Session, start: str = "2005-01-01"
) -> pd.Series:
    payload = _request_json(
        session,
        FRANKFURTER_TIMESERIES_URL.format(start=start),
        {"base": "USD", "symbols": "CNY"},
    )
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        raise RuntimeError("Frankfurter response has no rates")
    values: dict[pd.Timestamp, float] = {}
    for date_value, row in rates.items():
        if not isinstance(row, dict):
            continue
        value = pd.to_numeric(row.get("CNY"), errors="coerce")
        date = pd.to_datetime(date_value, errors="coerce")
        if pd.notna(date) and pd.notna(value) and float(value) > 0:
            values[pd.Timestamp(date).normalize()] = float(value)
    series = _completed_daily_series(pd.Series(values, dtype=float))
    if series.empty:
        raise RuntimeError("Frankfurter returned no valid USD/CNY observations")
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
    """Fetch USD/CNY from independent providers, stopping at the first usable source."""
    providers: list[tuple[str, Callable[[], pd.Series]]] = [
        (
            "Yahoo Finance CNY=X",
            lambda: fetch_yahoo_adjusted_close(session, "CNY=X", start),
        ),
        ("ECB eurofxref-hist 官方参考汇率", lambda: fetch_ecb_usd_cny(session)),
        (
            "Frankfurter ECB reference rates",
            lambda: fetch_frankfurter_usd_cny(session, start),
        ),
        ("FRED DEXCHUS", lambda: fetch_fred_series(session, "DEXCHUS", start)),
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
    fetch: Callable[[], pd.Series | tuple[pd.Series, str]],
) -> dict[str, object]:
    try:
        fetched = fetch()
        provider = source
        if isinstance(fetched, tuple):
            incoming, provider = fetched
        else:
            incoming = fetched
        merged = store.merge_and_write(name, incoming)
        return _status(
            name,
            "success",
            provider,
            f"{provider} 数据已合并到本地",
            merged,
            label=label,
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
            )
        except Exception:
            return _status(name, "error", source, str(exc), label=label)


def _refresh_remote_tasks(
    store: MarketStore,
    session: requests.Session,
    tasks: list[tuple[str, str, str, Callable[[], pd.Series | tuple[pd.Series, str]]]],
) -> list[dict[str, object]]:
    """Run remote tasks serially, retrying only failed tasks between rounds."""
    statuses: dict[str, dict[str, object]] = {}
    for name, label, source, fetch in tasks:
        statuses[name] = _update_remote_series(store, session, name, label, source, fetch)

    for retry_number in range(1, MAX_REFRESH_RETRIES + 1):
        failed_tasks = [task for task in tasks if statuses[task[0]]["status"] != "success"]
        if not failed_tasks:
            break
        time.sleep(REFRESH_RETRY_DELAY_SECONDS)
        for name, label, source, fetch in failed_tasks:
            retry_status = _update_remote_series(store, session, name, label, source, fetch)
            if retry_status["status"] != "success":
                retry_status = dict(retry_status)
                retry_status["message"] = (
                    f"{retry_status['message']}；已完成第 {retry_number}/{MAX_REFRESH_RETRIES} 次重试"
                )
            statuses[name] = retry_status

    return [statuses[name] for name, _, _, _ in tasks]


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
        fx_source = next(
            (str(item["source"]) for item in dependency_states if item["name"] == "usd_cny"),
            "本地 USD/CNY",
        )
        message = f"美元价格与 USD/CNY（{fx_source}）已合并为人民币序列"
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

    tasks: list[tuple[str, str, str, Callable[[], pd.Series | tuple[pd.Series, str]]]] = []
    domestic = [
        ("csi300", "510300"),
        ("dividend_low_vol", "512890"),
        ("star50", "588000"),
        ("long_bond", "511260"),
    ]
    for asset, code in domestic:
        tasks.append(
            (
                asset,
                ASSET_META[asset]["label"],
                ASSET_META[asset]["source"],
                lambda code=code: fetch_domestic_daily(session, code),
            )
        )

    tasks.extend(
        [
            (
                "usd_cny",
                "USD/CNY",
                "Yahoo Finance CNY=X -> ECB -> Frankfurter -> FRED",
                lambda: fetch_fx_usd_cny(session),
            ),
            (
                "qqq_usd",
                "QQQ美元价格",
                "Yahoo Finance QQQ",
                lambda: fetch_yahoo_adjusted_close(session, "QQQ"),
            ),
            (
                "gld_usd",
                "GLD美元价格",
                "Yahoo Finance GLD",
                lambda: fetch_yahoo_adjusted_close(session, "GLD"),
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
    store.append_refresh_log(result)
    return result
