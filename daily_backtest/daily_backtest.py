from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
DEFAULT_OUTPUT = HERE
START_DATE = pd.Timestamp("2016-08-15")
INITIAL_CAPITAL = 50_000.0
MA_DAYS = 250
BAND = 0.03
# No additional position-drift gate: every confirmed MA sell signal executes.
SELL_DRIFT_BAND = 0.0
COMMISSION_RATE = 0.00006
MINIMUM_COMMISSION = 0.30
RELEASED_TO_CASH_RATIO = 0.50
CHECK_FREQUENCIES = ("daily", "monthly", "monthly_10th")
SIGNAL_BASES = ("raw_price", "total_return")
DEFAULT_SIGNAL_BASIS = "raw_price"
DEFAULT_MONTHLY_CONTRIBUTION = 10_000.0
LOW_VOL_SOURCES = ("h20269", "512890")
DEFAULT_LOW_VOL_SOURCE = "h20269"
DEFAULT_CONFIRMATION_DAYS = 2

SSE_DAYK_URL = "http://yunhq.sse.com.cn:32041/v1/sh1/dayk/{code}"
SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_ANNOUNCEMENT_PAGE = "https://www.sse.com.cn/disclosure/fund/announcement/"
SSE_PDF_ROOT = "https://www.sse.com.cn"
NASDAQ_HISTORY_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
NASDAQ_DIVIDEND_URL = "https://api.nasdaq.com/api/quote/{symbol}/dividends?assetclass=etf"
GLD_ARCHIVE_URL = (
    "https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en&product=gld"
)
GLD_DISTRIBUTION_STATEMENT_URL = (
    "https://www.spdrgoldshares.com/media/GLD/file/singapore/"
    "GLD-Sing-Product-Highlights-Sheet-Sept-2025.pdf"
)
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
CSINDEX_HISTORY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"

BASE_WEIGHTS = {
    "dividend_low_vol": 0.30,
    "nasdaq100": 0.50,
    "gold": 0.10,
    "long_bond": 0.05,
    "cash": 0.05,
}
RISK_ASSETS = ["dividend_low_vol", "nasdaq100", "gold"]
ALL_ASSETS = ["dividend_low_vol", "nasdaq100", "gold", "long_bond"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_series(values: pd.Series) -> pd.Series:
    result = pd.to_numeric(values, errors="coerce")
    result.index = pd.to_datetime(result.index, errors="coerce").normalize()
    result = result[result.index.notna() & result.notna() & (result > 0)]
    return result[~result.index.duplicated(keep="last")].sort_index()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def series_digest(series: pd.Series) -> str:
    cleaned = clean_series(series)
    payload = "\n".join(
        f"{idx:%Y-%m-%d},{float(value):.12g}" for idx, value in cleaned.items()
    ).encode("utf-8")
    return sha256_bytes(payload)


def write_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, date_format="%Y-%m-%d")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_series(path: Path, column: str = "close") -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["date"])
    return clean_series(pd.Series(frame[column].to_numpy(), index=frame["date"]))


def completed(series: pd.Series) -> pd.Series:
    today = pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None).normalize()
    return clean_series(series[series.index < today])


def fetch_sse_daily(session: requests.Session, code: str) -> pd.Series:
    response = session.get(
        SSE_DAYK_URL.format(code=code),
        params={"begin": "-10000", "end": "-1", "period": "day"},
        headers={"Referer": "https://www.sse.com.cn/", "Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json().get("kline") or []
    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            date = pd.to_datetime(str(row[0]), format="%Y%m%d", errors="coerce")
            close = pd.to_numeric(row[4], errors="coerce")
            if pd.notna(date) and pd.notna(close) and float(close) > 0:
                dates.append(pd.Timestamp(date))
                closes.append(float(close))
    result = completed(pd.Series(closes, index=dates))
    if result.empty:
        raise RuntimeError(f"SSE returned no data for {code}")
    return result


def fetch_csindex_history(
    session: requests.Session,
    index_code: str,
    start: str = "2005-01-01",
) -> tuple[pd.Series, dict[str, Any]]:
    """Fetch a CSI index daily close from the official CSIndex endpoint."""
    url = CSINDEX_HISTORY_URL
    response = session.get(
        url,
        params={
            "indexCode": index_code,
            "startDate": pd.Timestamp(start).strftime("%Y%m%d"),
            "endDate": (pd.Timestamp.now(tz="Asia/Shanghai").date() + pd.Timedelta(days=1)).strftime("%Y%m%d"),
        },
        headers={"User-Agent": "daily-backtest-official-data/1.0", "Accept": "application/json,text/plain,*/*", "Referer": "https://www.csindex.com.cn/"},
        timeout=60,
    )
    response.raise_for_status()
    body_hash = sha256_bytes(response.content)
    rows = response.json().get("data") or []
    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = pd.to_datetime(str(row.get("tradeDate") or ""), format="%Y%m%d", errors="coerce")
        close = pd.to_numeric(row.get("close"), errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date))
            closes.append(float(close))
    result = completed(pd.Series(closes, index=dates, name=index_code))
    if result.empty:
        raise RuntimeError(f"CSI Index returned no data for {index_code}")
    return result, {
        "status": "verified",
        "url": response.url,
        "sha256": body_hash,
        "rows": len(result),
        "first_date": str(result.index[0].date()),
        "last_date": str(result.index[-1].date()),
        "return_type": "total_return_index",
        "message": "CSI H20269 is a total-return index; distributions are embedded in the index level and are not booked as a separate ETF cash dividend.",
        "checked_at": utc_now(),
    }


def fetch_nasdaq_history(session: requests.Session, symbol: str, start: str = "2005-01-01") -> pd.Series:
    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        response = session.get(
            NASDAQ_HISTORY_URL.format(symbol=symbol),
            params={
                "assetclass": "etf",
                "fromdate": start,
                "todate": (pd.Timestamp.now(tz="UTC").date() + pd.Timedelta(days=1)).isoformat(),
                "limit": 5000,
                "offset": offset,
            },
            headers={
                "User-Agent": "daily-backtest-official-data/1.0",
                "Accept": "application/json",
                "Origin": "https://www.nasdaq.com",
                "Referer": f"https://www.nasdaq.com/market-activity/etf/{symbol.lower()}/historical",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        table = data.get("tradesTable") if isinstance(data, dict) else None
        page = table.get("rows") if isinstance(table, dict) else None
        if not isinstance(page, list) or not page:
            break
        rows.extend(row for row in page if isinstance(row, dict))
        try:
            total = int(data.get("totalRecords"))
        except (TypeError, ValueError):
            total = offset + len(page)
        offset += len(page)
        if len(page) < 5000:
            break
    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for row in rows:
        date = pd.to_datetime(row.get("date"), format="%m/%d/%Y", errors="coerce")
        close = pd.to_numeric(str(row.get("close", "")).replace(",", ""), errors="coerce")
        if pd.notna(date) and pd.notna(close) and float(close) > 0:
            dates.append(pd.Timestamp(date))
            closes.append(float(close))
    result = completed(pd.Series(closes, index=dates))
    if result.empty:
        raise RuntimeError(f"Nasdaq returned no data for {symbol}")
    return result


def fetch_yahoo_chart(
    session: requests.Session,
    symbol: str,
    start: str = "2005-01-01",
) -> tuple[pd.Series, pd.Series, list[dict[str, Any]], dict[str, Any]]:
    """Fetch Yahoo's raw/adjusted chart and dividend events in one response.

    The v1test long-history builder uses this endpoint for QQQ.  We retain both
    close variants: raw close is the trading/signal input, while adjusted close
    is the total-return reference.  Yahoo's chart dividend events do not carry
    payment dates, so their cash date is explicitly recorded as the ex-date.
    """
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp())
    end_ts = int((pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)).timestamp())
    url = YAHOO_CHART_URL.format(symbol=requests.utils.quote(symbol, safe=""))
    params = {
        "period1": start_ts,
        "period2": end_ts,
        "interval": "1d",
        "events": "history,div",
        "includeAdjustedClose": "true",
    }
    response = session.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    body_hash = sha256_bytes(response.content)
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict) or not result.get("timestamp"):
        raise RuntimeError(f"Yahoo chart returned no history for {symbol}")

    index = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_localize(None).normalize()
    indicators = result.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    raw_values = quote.get("close")
    if raw_values is None:
        raise RuntimeError(f"Yahoo chart has no raw close for {symbol}")
    adjusted_values = (indicators.get("adjclose") or [{}])[0].get("adjclose")
    adjusted_available = adjusted_values is not None
    if adjusted_values is None:
        adjusted_values = raw_values

    raw = completed(pd.Series(raw_values, index=index, name=symbol))
    adjusted = completed(pd.Series(adjusted_values, index=index, name=symbol))
    if raw.empty or adjusted.empty:
        raise RuntimeError(f"Yahoo chart returned no usable close for {symbol}")

    events: list[dict[str, Any]] = []
    dividend_rows = ((result.get("events") or {}).get("dividends") or {})
    for item in dividend_rows.values():
        if not isinstance(item, dict):
            continue
        amount = pd.to_numeric(item.get("amount"), errors="coerce")
        timestamp = pd.to_numeric(item.get("date"), errors="coerce")
        if pd.isna(amount) or float(amount) <= 0 or pd.isna(timestamp):
            continue
        ex_date = pd.to_datetime(float(timestamp), unit="s", utc=True).tz_localize(None).normalize()
        events.append(
            {
                "asset": symbol,
                "ex_date": ex_date,
                "record_date": pd.NaT,
                "pay_date": ex_date,
                "amount_per_share": float(amount),
                "currency": "USD",
                "source_url": response.url,
                "source_sha256": body_hash,
                "source_type": "Yahoo Finance chart dividend event",
                "pay_date_assumed": True,
                "status": "verified_third_party_ex_date",
                "message": "Yahoo chart provides ex-date and amount but no payment date; cash is credited on ex-date.",
            }
        )
    events.sort(key=lambda event: (pd.Timestamp(event["ex_date"]), float(event["amount_per_share"])))
    return raw, adjusted, events, {
        "status": "verified" if adjusted_available else "warning",
        "url": response.url,
        "sha256": body_hash,
        "rows": len(raw),
        "dividend_rows": len(events),
        "first_date": str(raw.index[0].date()),
        "last_date": str(raw.index[-1].date()),
        "adjusted_close_available": adjusted_available,
        "checked_at": utc_now(),
    }


def merge_qqq_dividends(
    yahoo_events: list[dict[str, Any]],
    official_events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prefer official Nasdaq events on overlap and fill older history from Yahoo."""
    merged: dict[pd.Timestamp, dict[str, Any]] = {}
    for event in yahoo_events:
        merged[pd.Timestamp(event["ex_date"]).normalize()] = dict(event)
    for event in official_events:
        merged[pd.Timestamp(event["ex_date"]).normalize()] = dict(event)
    result = sorted(merged.values(), key=lambda event: pd.Timestamp(event["ex_date"]))
    yahoo_dates = {pd.Timestamp(event["ex_date"]).normalize() for event in yahoo_events}
    official_dates = {pd.Timestamp(event["ex_date"]).normalize() for event in official_events}
    overlap = yahoo_dates & official_dates
    return result, {
        "status": "verified" if result else "warning",
        "rows": len(result),
        "yahoo_rows": len(yahoo_events),
        "official_rows": len(official_events),
        "yahoo_only_rows": len(yahoo_dates - official_dates),
        "official_only_rows": len(official_dates - yahoo_dates),
        "overlap_rows": len(overlap),
        "message": "Official Nasdaq dividend rows take precedence on overlapping ex-dates; Yahoo fills the pre-official history.",
    }


def fetch_gld(session: requests.Session) -> pd.Series:
    response = session.get(GLD_ARCHIVE_URL, headers={"User-Agent": "daily-backtest-official-data/1.0"}, timeout=60)
    response.raise_for_status()
    frame = pd.read_excel(__import__("io").BytesIO(response.content), sheet_name="US GLD Historical Archive")
    if "Date" not in frame or "Closing Price" not in frame:
        raise RuntimeError("GLD archive lacks Date/Closing Price")
    result = completed(pd.Series(frame["Closing Price"].to_numpy(), index=pd.to_datetime(frame["Date"], errors="coerce")))
    if result.empty:
        raise RuntimeError("GLD returned no data")
    return result


def fetch_fred_fx(session: requests.Session) -> pd.Series:
    response = session.get(
        FRED_URL,
        params={"id": "DEXCHUS", "cosd": "2005-01-01", "coed": (pd.Timestamp.now().date() + pd.Timedelta(days=1)).isoformat()},
        timeout=30,
    )
    response.raise_for_status()
    frame = pd.read_csv(__import__("io").StringIO(response.text))
    date_column = "observation_date" if "observation_date" in frame else "DATE"
    result = clean_series(pd.Series(pd.to_numeric(frame["DEXCHUS"], errors="coerce").to_numpy(), index=pd.to_datetime(frame[date_column], errors="coerce")))
    result = completed(result)
    if result.empty:
        raise RuntimeError("FRED DEXCHUS returned no data")
    return result


def parse_usd_amount(raw: Any) -> float | None:
    value = pd.to_numeric(str(raw).replace("$", "").replace(",", "").strip(), errors="coerce")
    return float(value) if pd.notna(value) and float(value) > 0 else None


def fetch_nasdaq_dividends(session: requests.Session, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = NASDAQ_DIVIDEND_URL.format(symbol=symbol)
    response = session.get(
        url,
        headers={"User-Agent": "daily-backtest-official-data/1.0", "Accept": "application/json", "Origin": "https://www.nasdaq.com"},
        timeout=30,
    )
    response.raise_for_status()
    body_hash = sha256_bytes(response.content)
    data = response.json().get("data") or {}
    divs = data.get("dividends") if isinstance(data, dict) else None
    rows = divs.get("rows") if isinstance(divs, dict) else None
    events: list[dict[str, Any]] = []
    for row in rows or []:
        amount = parse_usd_amount(row.get("amount"))
        ex_date = pd.to_datetime(row.get("exOrEffDate"), errors="coerce")
        if amount is None or pd.isna(ex_date):
            continue
        events.append(
            {
                "asset": symbol,
                "ex_date": pd.Timestamp(ex_date).normalize(),
                "record_date": pd.to_datetime(row.get("recordDate"), errors="coerce"),
                "pay_date": pd.to_datetime(row.get("paymentDate"), errors="coerce"),
                "amount_per_share": amount,
                "currency": str(row.get("currency") or "USD"),
                "source_url": url,
                "source_sha256": body_hash,
                "status": "verified",
            }
        )
    status = "verified" if events else "verified_no_distribution"
    return events, {"status": status, "url": url, "sha256": body_hash, "rows": len(events), "checked_at": utc_now()}


def fetch_gld_distribution_status(session: requests.Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response = session.get(
        GLD_DISTRIBUTION_STATEMENT_URL,
        headers={"User-Agent": "daily-backtest-official-data/1.0", "Accept": "application/pdf"},
        timeout=60,
    )
    response.raise_for_status()
    body_hash = sha256_bytes(response.content)
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to verify the official GLD distribution statement") from exc
    document = fitz.open(stream=response.content, filetype="pdf")
    text = " ".join(page.get_text() for page in document).lower()
    normalized = " ".join(text.split())
    statement = "no distributions have been made by the trust since inception"
    if statement not in normalized:
        return [], {
            "status": "warning",
            "url": GLD_DISTRIBUTION_STATEMENT_URL,
            "sha256": body_hash,
            "rows": 0,
            "checked_at": utc_now(),
            "message": "The official GLD distribution statement did not contain the expected no-distribution text; signal is paused.",
        }
    return [], {
        "status": "verified_no_distribution",
        "url": GLD_DISTRIBUTION_STATEMENT_URL,
        "sha256": body_hash,
        "rows": 0,
        "checked_at": utc_now(),
        "message": "The issuer's official product statement says no distributions have been made since inception.",
    }


DATE_RE = re.compile(r"(?<!\d)(20\d{2})\D{0,8}([01]?\d)\D{0,8}([0-3]?\d)(?!\d)")
DECIMAL_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})*\.\d{1,6})(?!\d)")


def parse_sse_distribution_pdf(content: bytes, announcement_date: str, url: str, code: str) -> dict[str, Any]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to parse SSE distribution PDFs") from exc
    document = fitz.open(stream=content, filetype="pdf")
    text = "\n".join(page.get_text() for page in document)
    lines = text.splitlines()
    amount_index: int | None = None
    amount_per_10: float | None = None
    for index, line in enumerate(lines):
        if not re.search(r"\d{1,3}(?:,\d{3})+\.\d+", line):
            continue
        for candidate_line in lines[index + 1 : index + 7]:
            for token in DECIMAL_RE.findall(candidate_line):
                value = float(token.replace(",", ""))
                if 0 < value < 1000:
                    amount_per_10 = value
                    amount_index = index
                    break
            if amount_per_10 is not None:
                break
        if amount_per_10 is not None:
            break
    if amount_per_10 is None or amount_index is None:
        raise RuntimeError(f"cannot locate distribution amount in {url}")

    dates: list[pd.Timestamp] = []
    for line in lines[amount_index + 1 :]:
        for year, month, day in DATE_RE.findall(line):
            value = pd.Timestamp(year=int(year), month=int(month), day=int(day))
            if value not in dates:
                dates.append(value)
    if len(dates) < 2:
        raise RuntimeError(f"cannot locate record/ex dates in {url}")
    record_date = dates[0]
    ex_date = dates[1]
    pay_date = next((date for date in dates[2:] if date > ex_date), dates[2] if len(dates) > 2 else pd.NaT)
    return {
        "asset": code,
        "announcement_date": pd.Timestamp(announcement_date).normalize(),
        "ex_date": ex_date,
        "record_date": record_date,
        "pay_date": pay_date,
        "amount_per_share": amount_per_10 / 10.0,
        "amount_per_10": amount_per_10,
        "currency": "CNY",
        "source_url": url,
        "source_sha256": sha256_bytes(content),
        "status": "verified",
    }


def fetch_sse_dividends(session: requests.Session, code: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "100",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "type": "inParams",
        "sqlId": "COMMON_PL_JJXX_JJGG_NEW_L",
        "TITLE": "分红",
        "SECURITY_CODE": code,
        "BULLETIN_TYPE": "fund01,fund02,fund03,fund04,fund05,fund06",
        "START_DATE": "2017-01-01",
        "END_DATE": pd.Timestamp.now().strftime("%Y-%m-%d"),
        "DATE_DESC": "1",
        "DATE_ASC": "",
        "CODE_DESC": "",
        "CODE_ASC": "",
    }
    headers = {"Referer": SSE_ANNOUNCEMENT_PAGE, "User-Agent": "daily-backtest-official-data/1.0", "Accept": "application/json,text/plain,*/*"}
    response = session.get(SSE_QUERY_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    query_hash = sha256_bytes(response.content)
    payload = response.json()
    result = payload.get("result") or []
    total_pages = int((payload.get("pageHelp") or {}).get("pageCount") or 1)
    all_rows = list(result)
    for page in range(2, total_pages + 1):
        page_params = dict(params)
        page_params.update({"pageHelp.pageNo": str(page), "pageHelp.beginPage": str(page), "pageHelp.endPage": str(page)})
        page_response = session.get(SSE_QUERY_URL, params=page_params, headers=headers, timeout=30)
        page_response.raise_for_status()
        all_rows.extend(page_response.json().get("result") or [])
    if not all_rows:
        return [], {"status": "warning", "url": response.url, "sha256": query_hash, "rows": 0, "checked_at": utc_now(), "message": "No official SSE distribution announcement was returned; signal is paused."}

    events: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for row in all_rows:
        relative_url = str(row.get("URL") or "")
        if not relative_url:
            parse_errors.append("missing PDF URL")
            continue
        pdf_url = relative_url if relative_url.startswith("http") else SSE_PDF_ROOT + relative_url
        try:
            pdf_response = session.get(pdf_url, headers={"Referer": SSE_ANNOUNCEMENT_PAGE, "User-Agent": headers["User-Agent"]}, timeout=60)
            pdf_response.raise_for_status()
            events.append(parse_sse_distribution_pdf(pdf_response.content, str(row.get("SSEDATE")), pdf_url, code))
        except Exception as exc:
            parse_errors.append(f"{pdf_url}: {exc}")
    if not events:
        return [], {"status": "warning", "url": response.url, "sha256": query_hash, "rows": 0, "checked_at": utc_now(), "message": "SSE announcements were found but no distribution PDF could be parsed; signal is paused.", "errors": parse_errors}
    events = list({(str(e["ex_date"]), float(e["amount_per_share"])): e for e in events}.values())
    events.sort(key=lambda event: event["ex_date"])
    status = "verified" if not parse_errors else "warning"
    return events, {"status": status, "url": response.url, "sha256": query_hash, "rows": len(events), "checked_at": utc_now(), "errors": parse_errors}


def apply_dividends(raw: pd.Series, events: list[dict[str, Any]]) -> tuple[pd.Series, list[dict[str, Any]]]:
    adjusted = raw.copy()
    applied: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: pd.Timestamp(item["ex_date"])):
        ex_date = pd.Timestamp(event["ex_date"]).normalize()
        if ex_date < raw.index[0]:
            item = dict(event)
            item["status"] = "before_history_ignored"
            applied.append(item)
            continue
        available = raw.index[raw.index >= ex_date]
        if len(available) == 0:
            event = dict(event)
            event["status"] = "warning_after_history"
            applied.append(event)
            continue
        price_date = available[0]
        ex_price = float(raw.loc[price_date])
        amount = float(event["amount_per_share"])
        factor = ex_price / (ex_price + amount)
        adjusted.loc[adjusted.index < price_date] *= factor
        item = dict(event)
        item.update({"adjustment_price_date": price_date, "adjustment_factor": factor, "status": "applied"})
        applied.append(item)
    return adjusted, applied


def combine_fx(usd: pd.Series, fx: pd.Series) -> pd.Series:
    frame = usd.rename("usd").to_frame().join(fx.rename("fx"), how="left")
    frame["fx"] = frame["fx"].ffill()
    frame = frame.dropna()
    return clean_series(frame["usd"] * frame["fx"])


def load_long_bond_effective(official: pd.Series) -> tuple[pd.Series, dict[str, Any]]:
    proxy_path = PROJECT_ROOT.parent / "v1test" / "data" / "long_bond_cny.csv"
    if not proxy_path.exists():
        return official, {"status": "warning", "message": "No pre-ETF long-bond proxy was found."}
    proxy = read_series(proxy_path)
    official_start = official.index[0]
    anchor = proxy.loc[proxy.index.intersection([official_start])]
    if anchor.empty:
        return official, {"status": "warning", "message": "Long-bond proxy has no transition anchor."}
    scale = float(official.iloc[0]) / float(anchor.iloc[0])
    before = proxy.loc[proxy.index < official_start] * scale
    effective = clean_series(pd.concat([before, official]))
    return effective, {
        "status": "success",
        "proxy_path": str(proxy_path),
        "proxy_scale_at_official_start": scale,
        "official_start": official_start,
        "message": "Pre-2017-08-24 long-bond proxy stitched to the first official 511260 close; asset return statistics still start at official ETF data.",
    }


def prepare_data(
    output_root: Path,
    strategy_start: pd.Timestamp = START_DATE,
    low_vol_source: str = DEFAULT_LOW_VOL_SOURCE,
) -> dict[str, Any]:
    strategy_start = pd.Timestamp(strategy_start).normalize()
    if low_vol_source not in LOW_VOL_SOURCES:
        raise ValueError(f"Unsupported low-volatility source: {low_vol_source}")
    data_dir = output_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"Accept": "application/json,text/plain,*/*"})

    # v1test's long-history source is Yahoo Finance.  Keep the raw close for
    # trading/signals and the adjusted close for the total-return reference.
    qqq_usd, qqq_adjusted_usd, qqq_yahoo_events, qqq_yahoo_meta = fetch_yahoo_chart(session, "QQQ")
    # Nasdaq remains a live cross-check for the overlapping native ETF history;
    # it is not used to backfill the 2005-2016 period that Nasdaq does not serve.
    qqq_official_usd = fetch_nasdaq_history(session, "QQQ")
    gld_usd = fetch_gld(session)
    fx = fetch_fred_fx(session)
    if low_vol_source == "h20269":
        # H20269 is the CSI Dividend Low Volatility total-return index. Its
        # distributions are embedded in the index level, so it replaces the ETF
        # price/dividend ledger for this sleeve.
        dividend_low_vol, low_vol_meta = fetch_csindex_history(session, "H20269")
    else:
        dividend_low_vol = fetch_sse_daily(session, "512890")
        low_vol_meta = {
            "status": "verified",
            "url": SSE_DAYK_URL.format(code="512890"),
            "rows": len(dividend_low_vol),
            "first_date": str(dividend_low_vol.index[0].date()),
            "last_date": str(dividend_low_vol.index[-1].date()),
            "return_type": "raw_etf_close",
            "message": "SSE 512890 raw ETF close; no total-return adjustment is applied to the signal series.",
            "checked_at": utc_now(),
        }
    long_bond_official = fetch_sse_daily(session, "511260")
    long_bond_effective, proxy_info = load_long_bond_effective(long_bond_official)

    qqq_official_events, qqq_official_div_status = fetch_nasdaq_dividends(session, "QQQ")
    qqq_events, qqq_div_merge = merge_qqq_dividends(qqq_yahoo_events, qqq_official_events)
    qqq_div_status = {
        **qqq_official_div_status,
        "status": qqq_div_merge["status"],
        "yahoo_chart": qqq_yahoo_meta,
        "merge": qqq_div_merge,
        "third_party_url": qqq_yahoo_meta["url"],
        "third_party_sha256": qqq_yahoo_meta["sha256"],
        "message": qqq_div_merge["message"],
    }
    gld_events, gld_div_status = fetch_gld_distribution_status(session)
    if low_vol_source == "h20269":
        low_vol_events: list[dict[str, Any]] = []
        low_vol_div_status = {
            **low_vol_meta,
            "status": "verified",
            "rows": 0,
            "return_type": "total_return_index",
            "message": "H20269 is a total-return index; distributions are embedded in the index level and are not booked as a separate ETF cash dividend.",
        }
    else:
        low_vol_events, low_vol_div_status = fetch_sse_dividends(session, "512890")
    bond_events, bond_div_status = fetch_sse_dividends(session, "511260")

    # Yahoo supplies the adjusted close directly.  We still run the event
    # ledger through apply_dividends so every event gets an auditable factor;
    # the direct adjusted series is the MA/total-return input and avoids a
    # second adjustment pass over Yahoo's already-adjusted history.
    _, qqq_applied = apply_dividends(qqq_usd, qqq_events)
    qqq_signal_usd = qqq_adjusted_usd
    bond_signal, bond_applied = apply_dividends(long_bond_official, bond_events)
    bond_signal_effective = clean_series(pd.concat([long_bond_effective.loc[long_bond_effective.index < long_bond_official.index[0]], bond_signal]))
    low_vol_signal, low_vol_applied = apply_dividends(dividend_low_vol, low_vol_events)
    gld_signal_usd, gld_applied = apply_dividends(gld_usd, gld_events)

    raw_cny = {
        "dividend_low_vol": dividend_low_vol,
        "nasdaq100": combine_fx(qqq_usd, fx),
        "gold": combine_fx(gld_usd, fx),
        "long_bond": long_bond_effective,
    }
    signal_cny = {
        "dividend_low_vol": low_vol_signal,
        "nasdaq100": combine_fx(qqq_signal_usd, fx),
        "gold": combine_fx(gld_signal_usd, fx),
        "long_bond": bond_signal_effective,
    }
    event_status = {
        "dividend_low_vol": low_vol_div_status,
        "nasdaq100": qqq_div_status,
        "gold": gld_div_status,
        "long_bond": bond_div_status,
    }
    overlap = qqq_usd.rename("yahoo").to_frame().join(qqq_official_usd.rename("nasdaq"), how="inner").dropna()
    if overlap.empty:
        qqq_overlap_validation = {"status": "warning", "rows": 0, "message": "Yahoo and Nasdaq QQQ histories have no overlapping dates."}
    else:
        relative_error = ((overlap["yahoo"] - overlap["nasdaq"]).abs() / overlap["nasdaq"].abs()).replace([np.inf, -np.inf], np.nan).dropna()
        qqq_overlap_validation = {
            "status": "verified" if not relative_error.empty and float(relative_error.median()) <= 0.02 else "warning",
            "rows": len(overlap),
            "median_relative_error": float(relative_error.median()) if not relative_error.empty else None,
            "max_relative_error": float(relative_error.max()) if not relative_error.empty else None,
            "message": "Yahoo raw close is used; Nasdaq is retained as an overlapping live cross-check.",
        }

    raw_sources = {
        "dividend_low_vol": low_vol_meta["url"],
        "nasdaq100": qqq_yahoo_meta["url"],
        "nasdaq100_official_validation": NASDAQ_HISTORY_URL.format(symbol="QQQ") + "?assetclass=etf&fromdate=2005-01-01&todate=2100-01-01&limit=5000",
        "gold": GLD_ARCHIVE_URL,
        "long_bond": SSE_DAYK_URL.format(code="511260"),
        "usd_cny": FRED_URL + "?id=DEXCHUS",
    }
    raw_series = {
        "raw_qqq_usd": qqq_usd,
        "adjusted_qqq_usd": qqq_adjusted_usd,
        "official_validation_qqq_usd": qqq_official_usd,
        "raw_gld_usd": gld_usd,
        "raw_usd_cny": fx,
        "raw_dividend_low_vol": dividend_low_vol,
        "raw_long_bond_official": long_bond_official,
        "raw_long_bond_effective": long_bond_effective,
        "raw_nasdaq100_cny": raw_cny["nasdaq100"],
        "raw_gold_cny": raw_cny["gold"],
    }
    for name, series in raw_series.items():
        write_frame(data_dir / f"{name}.csv", pd.DataFrame({"date": series.index, "close": series.to_numpy()}))

    applied_by_asset = {
        "dividend_low_vol": low_vol_applied,
        "nasdaq100": qqq_applied,
        "gold": gld_applied,
        "long_bond": bond_applied,
    }
    for asset in ALL_ASSETS:
        frame = pd.DataFrame({"date": raw_cny[asset].index, "raw_close_cny": raw_cny[asset].to_numpy()})
        signal = signal_cny[asset].reindex(raw_cny[asset].index)
        frame["total_return_close_cny"] = signal.to_numpy()
        frame["ma250"] = signal.rolling(MA_DAYS, min_periods=MA_DAYS).mean().to_numpy()
        frame["signal_valid"] = event_status[asset]["status"] in {"verified", "verified_no_distribution"}
        write_frame(data_dir / f"signal_{asset}.csv", frame)

    event_rows: list[dict[str, Any]] = []
    for asset, events in applied_by_asset.items():
        for event in events:
            event_rows.append({"asset": asset, **{key: value for key, value in event.items() if key not in {"asset"}}})
        if not events:
            status = event_status[asset]
            event_rows.append({"asset": asset, "source_url": status.get("url"), "source_sha256": status.get("sha256"), "status": status.get("status"), "message": status.get("message", "")})
    event_columns = ["asset", "announcement_date", "ex_date", "record_date", "pay_date", "amount_per_share", "amount_per_10", "currency", "source_url", "source_sha256", "source_type", "pay_date_assumed", "adjustment_price_date", "adjustment_factor", "status", "message"]
    events_frame = pd.DataFrame(event_rows)
    for column in event_columns:
        if column not in events_frame:
            events_frame[column] = pd.NA
    write_frame(data_dir / "dividends.csv", events_frame[event_columns].sort_values(["asset", "ex_date"], na_position="last"))

    manifest_sources = []
    for key, series in raw_series.items():
        source_key = {"raw_qqq_usd": "nasdaq100", "adjusted_qqq_usd": "nasdaq100", "official_validation_qqq_usd": "nasdaq100_official_validation", "raw_gld_usd": "gold", "raw_usd_cny": "usd_cny", "raw_dividend_low_vol": "dividend_low_vol", "raw_long_bond_official": "long_bond"}.get(key)
        manifest_sources.append({"name": key, "url": raw_sources.get(source_key, "local stitched series"), "checked_at": utc_now(), "rows": len(series), "first_date": str(series.index[0].date()), "last_date": str(series.index[-1].date()), "series_sha256": series_digest(series)})
    manifest = {
        "generated_at": utc_now(),
        "strategy_start": str(strategy_start.date()),
        "sources": manifest_sources,
        "dividend_sources": event_status,
        "dividend_events": event_rows,
        "proxy": proxy_info,
        "qqq_yahoo": qqq_yahoo_meta,
        "qqq_overlap_validation": qqq_overlap_validation,
        "qqq_dividend_merge": qqq_div_merge,
        "official_urls": {"csindex_h20269_history": CSINDEX_HISTORY_URL, "sse_announcement_page": SSE_ANNOUNCEMENT_PAGE, "sse_query": SSE_QUERY_URL, "qqq_dividends": NASDAQ_DIVIDEND_URL.format(symbol="QQQ"), "gld_distribution_statement": GLD_DISTRIBUTION_STATEMENT_URL},
        "low_vol_source": low_vol_source,
    }
    write_json(data_dir / "source_manifest.json", manifest)
    official_starts = {
        "dividend_low_vol": dividend_low_vol.index[0],
        "nasdaq100": qqq_official_usd.index[0],
        "gold": raw_cny["gold"].index[0],
        "long_bond": long_bond_official.index[0],
    }
    write_frame(
        data_dir / "coverage.csv",
        pd.DataFrame(
            [
                {
                    "asset": asset,
                    "official_native_start": str(official_starts[asset].date()),
                    "effective_series_start": str(raw_cny[asset].index[0].date()),
                    "native_end": str(raw_cny[asset].index[-1].date()),
                    "event_status": event_status[asset]["status"],
                    "signal_paused": event_status[asset]["status"] == "warning",
                }
                for asset in ALL_ASSETS
            ]
        ),
    )
    return {
        "raw_cny": raw_cny,
        "raw_official": {"long_bond": long_bond_official},
        "signal_cny": signal_cny,
        "events": applied_by_asset,
        "event_status": event_status,
        "fx": fx,
        "manifest": manifest,
    }


def fee_for(gross: float) -> float:
    return max(gross * COMMISSION_RATE, MINIMUM_COMMISSION) if gross > 0 else 0.0


def check_dates_for_frequency(calendar: pd.DatetimeIndex, frequency: str) -> pd.DatetimeIndex:
    """Return the dates on which the strategy is allowed to evaluate signals."""
    if frequency == "daily":
        return calendar
    if frequency == "monthly":
        calendar_series = pd.Series(calendar, index=calendar)
        month_end = calendar_series.groupby(calendar.to_period("M")).max()
        return pd.DatetimeIndex(month_end.to_numpy())
    if frequency == "monthly_10th":
        # Rebalance on the first available trading day on/after the 10th of
        # each month. The signal checkpoint is the immediately preceding
        # completed trading day; the normal next-date execution then places
        # the order on that 10th-ish trading day.
        calendar = pd.DatetimeIndex(calendar).sort_values().unique()
        if len(calendar) < 2:
            return pd.DatetimeIndex([])
        operation_dates: list[pd.Timestamp] = []
        periods = calendar.to_period("M").unique()
        for period in periods:
            target = period.start_time + pd.Timedelta(days=9)
            candidates = calendar[calendar >= target]
            if len(candidates) == 0:
                continue
            operation_date = pd.Timestamp(candidates[0])
            previous = calendar[calendar < operation_date]
            if len(previous) > 0:
                operation_dates.append(pd.Timestamp(previous[-1]))
        return pd.DatetimeIndex(operation_dates).drop_duplicates()
    raise ValueError(f"Unsupported check frequency: {frequency}")


def monthly_contribution_dates(calendar: pd.DatetimeIndex, amount: float) -> dict[pd.Timestamp, float]:
    """Map each calendar month to its first available portfolio date."""
    if amount < 0:
        raise ValueError("Monthly contribution cannot be negative")
    calendar = pd.DatetimeIndex(calendar).sort_values().unique()
    if amount == 0 or len(calendar) == 0:
        return {}
    dates = pd.Series(calendar, index=calendar).groupby(calendar.to_period("M")).min()
    return {pd.Timestamp(date): float(amount) for date in dates.to_numpy()}


def resolve_signal_inputs(data: dict[str, Any], signal_basis: str) -> tuple[dict[str, pd.Series], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    if signal_basis not in SIGNAL_BASES:
        raise ValueError(f"Unsupported signal basis: {signal_basis}")
    if signal_basis == "total_return":
        return data["signal_cny"], data["events"], data["event_status"]

    raw_cny: dict[str, pd.Series] = data["raw_cny"]
    raw_status: dict[str, dict[str, Any]] = {}
    for asset in ALL_ASSETS:
        original = dict(data["event_status"].get(asset, {}))
        raw_status[asset] = {
            **original,
            "status": "verified_raw_price_only",
            "original_event_status": original.get("status"),
            "message": "Signal intentionally uses raw official price/NAV; dividend events are ignored only for the signal, while the verified cash ledger remains available for final NAV.",
        }
    # Signal prices are raw, but the verified event ledger remains available
    # for final NAV/cash accounting.
    return raw_cny, data["events"], raw_status


def add_flow_adjusted_metrics(nav_frame: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    """Add account returns and TWR metrics while excluding external contributions."""
    frame = nav_frame.copy()
    contributions = frame["contribution_cny"] if "contribution_cny" in frame else pd.Series(0.0, index=frame.index)
    frame["contribution_cny"] = pd.to_numeric(contributions, errors="coerce").fillna(0.0)
    frame["account_daily_return"] = frame["nav_cny"].pct_change().fillna(0.0)
    previous_nav = frame["nav_cny"].shift(1)
    base_nav = previous_nav.fillna(float(initial_capital))
    factors = (frame["nav_cny"] - frame["contribution_cny"]) / base_nav
    factors = factors.where(base_nav > 0, 1.0).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    frame["twr_daily_return"] = factors - 1.0
    frame["twr_index"] = factors.cumprod()
    frame["twr_peak_index"] = frame["twr_index"].cummax()
    frame["drawdown"] = frame["twr_index"] / frame["twr_peak_index"] - 1.0
    # Keep the historical output column name, but make it the flow-adjusted
    # return so a monthly deposit is never counted as investment profit.
    frame["daily_return"] = frame["twr_daily_return"]
    return frame


def drawdown_recovery_metrics(values: pd.Series, dates: pd.Series | pd.DatetimeIndex) -> dict[str, Any]:
    """Return the trough, prior peak and first date back at that peak."""
    series = pd.to_numeric(values, errors="coerce").reset_index(drop=True)
    date_index = pd.DatetimeIndex(pd.to_datetime(dates, errors="coerce")).normalize()
    valid = series.notna() & pd.Series(date_index).notna()
    series = series[valid].reset_index(drop=True)
    date_index = date_index[valid.to_numpy()]
    if series.empty:
        return {
            "max_drawdown": np.nan,
            "peak_date": None,
            "trough_date": None,
            "recovery_date": None,
            "recovery_days": None,
        }
    peak = series.cummax()
    drawdown = series / peak - 1.0
    trough_pos = int(drawdown.to_numpy().argmin())
    trough_value = float(drawdown.iloc[trough_pos])
    peak_value = float(peak.iloc[trough_pos])
    peak_positions = np.flatnonzero(np.isclose(series.iloc[: trough_pos + 1].to_numpy(), peak_value, rtol=1e-12, atol=1e-12))
    peak_pos = int(peak_positions[-1]) if len(peak_positions) else 0
    recovery_positions = np.flatnonzero(series.iloc[trough_pos:].to_numpy() >= peak_value * (1.0 - 1e-12))
    recovery_pos = trough_pos + int(recovery_positions[0]) if len(recovery_positions) else None
    return {
        "max_drawdown": trough_value,
        "peak_date": str(pd.Timestamp(date_index[peak_pos]).date()),
        "trough_date": str(pd.Timestamp(date_index[trough_pos]).date()),
        "recovery_date": str(pd.Timestamp(date_index[recovery_pos]).date()) if recovery_pos is not None else None,
        "recovery_days": int((date_index[recovery_pos] - date_index[trough_pos]).days) if recovery_pos is not None else None,
    }


def annual_metrics_from_nav(nav_frame: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    """Return calendar-year TWR, annualized return, drawdown and cash-flow metrics."""
    frame = add_flow_adjusted_metrics(nav_frame, initial_capital)
    rows: list[dict[str, Any]] = []
    cumulative_contributions = 0.0
    previous_account = float(initial_capital)
    for year, group in frame.groupby(frame["date"].dt.year, sort=True):
        group = group.copy()
        annual_factor = float((1.0 + group["twr_daily_return"]).prod())
        annual_return = annual_factor - 1.0
        elapsed_days = max((group["date"].iloc[-1] - group["date"].iloc[0]).days, 1)
        annualized_return = float(annual_factor ** (365.25 / elapsed_days) - 1.0)
        annual_contribution = float(group["contribution_cny"].sum())
        ending_nav = float(group["nav_cny"].iloc[-1])
        annual_profit = ending_nav - previous_account - annual_contribution
        cumulative_contributions += annual_contribution
        internal_peak = group["twr_index"].cummax()
        internal_drawdown = group["twr_index"] / internal_peak - 1.0
        annual_recovery = drawdown_recovery_metrics(group["twr_index"], group["date"])
        rows.append(
            {
                "year": int(year),
                "start_date": str(group["date"].iloc[0].date()),
                "end_date": str(group["date"].iloc[-1].date()),
                "starting_nav_cny": previous_account,
                "ending_nav_cny": ending_nav,
                "annual_contribution_cny": annual_contribution,
                "cumulative_contributions_cny": cumulative_contributions,
                "annual_profit_cny": annual_profit,
                "cumulative_profit_cny": ending_nav - initial_capital - cumulative_contributions,
                "annual_return": annual_return,
                "annualized_return": annualized_return,
                "annual_max_drawdown": float(internal_drawdown.min()),
                "annual_drawdown_from_all_time_peak": float(group["drawdown"].min()),
                "annual_max_drawdown_peak_date": annual_recovery["peak_date"],
                "annual_max_drawdown_date": annual_recovery["trough_date"],
                "annual_max_drawdown_recovery_date": annual_recovery["recovery_date"],
                "annual_max_drawdown_recovery_days": annual_recovery["recovery_days"],
                "twr_index_end": float(group["twr_index"].iloc[-1]),
            }
        )
        previous_account = ending_nav
    return pd.DataFrame(rows)


def run_backtest(
    data: dict[str, Any],
    mode: str,
    output_root: Path,
    initial_capital: float = INITIAL_CAPITAL,
    frequency: str = "daily",
    signal_basis: str = DEFAULT_SIGNAL_BASIS,
    monthly_contribution: float = DEFAULT_MONTHLY_CONTRIBUTION,
    start_date: pd.Timestamp = START_DATE,
    confirmation_days: int = DEFAULT_CONFIRMATION_DAYS,
    released_to_cash_ratio: float = RELEASED_TO_CASH_RATIO,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if mode not in {"sell_all", "sell_half"}:
        raise ValueError(f"Unsupported sell mode: {mode}")
    if frequency not in CHECK_FREQUENCIES:
        raise ValueError(f"Unsupported check frequency: {frequency}")
    if monthly_contribution < 0:
        raise ValueError("Monthly contribution cannot be negative")
    if confirmation_days < 1:
        raise ValueError("confirmation_days must be at least 1")
    if not 0.0 <= released_to_cash_ratio <= 1.0:
        raise ValueError("released_to_cash_ratio must be between 0 and 1")
    start_date = pd.Timestamp(start_date).normalize()
    raw_cny: dict[str, pd.Series] = data["raw_cny"]
    signal_cny, events, event_status = resolve_signal_inputs(data, signal_basis)
    fx: pd.Series = data["fx"]
    raw_matrix = pd.concat({asset: series for asset, series in raw_cny.items()}, axis=1).sort_index()
    calendar = raw_matrix.index[raw_matrix.index >= start_date]
    check_dates = set(check_dates_for_frequency(calendar, frequency))
    contribution_schedule = monthly_contribution_dates(calendar, monthly_contribution)
    mark_matrix = raw_matrix.ffill()
    ma_series = {
        asset: signal_cny[asset].rolling(MA_DAYS, min_periods=MA_DAYS).mean()
        for asset in RISK_ASSETS
    }
    risk_state = {asset: True for asset in RISK_ASSETS}
    initialized = {asset: False for asset in RISK_ASSETS}
    below = {asset: 0 for asset in RISK_ASSETS}
    above = {asset: 0 for asset in RISK_ASSETS}
    pending: dict[str, dict[str, Any]] = {}
    # A drift-gated sell signal can turn the sleeve off before its weight is
    # large enough to sell. Keep watching the sleeve on later check dates so
    # the deferred sale is not silently lost.
    gate_waiting = {asset: False for asset in RISK_ASSETS}
    units = {asset: 0.0 for asset in ALL_ASSETS}
    cash = initial_capital * BASE_WEIGHTS["cash"]
    trades: list[dict[str, Any]] = []
    signal_log: list[dict[str, Any]] = []
    nav_rows: list[dict[str, Any]] = []
    dividend_cash_total = 0.0
    trade_cost_total = 0.0
    pending_dividend_cash: dict[pd.Timestamp, float] = {}

    def price_on(date: pd.Timestamp, asset: str, for_trade: bool = False) -> float | None:
        if for_trade:
            value = raw_cny[asset].get(date)
        else:
            value = mark_matrix.at[date, asset] if date in mark_matrix.index else np.nan
        return float(value) if pd.notna(value) and float(value) > 0 else None

    def nav_at(date: pd.Timestamp) -> float:
        total = cash
        for asset in ALL_ASSETS:
            price = price_on(date, asset)
            if price is not None:
                total += units[asset] * price
        return float(total)

    def execute_order(
        date: pd.Timestamp,
        asset: str,
        gross_target: float,
        side: str,
        reason: str,
        cash_floor: float | None = None,
    ) -> float:
        nonlocal cash, trade_cost_total
        price = price_on(date, asset, for_trade=True)
        if price is None or gross_target <= 0:
            return 0.0
        if side == "buy":
            # Ordinary buys may use only cash above the portfolio's cash target.
            # A sale-funded bond purchase passes its pre-sale cash as the floor,
            # so it cannot spend unrelated cash while reinvesting the proceeds.
            protected_cash = max(0.0, float(cash_floor or 0.0))
            available_cash = max(0.0, cash - protected_cash)
            gross = min(float(gross_target), max(0.0, (available_cash - MINIMUM_COMMISSION) / (1 + COMMISSION_RATE)))
            if gross <= 0:
                return 0.0
            units_change = gross / price
            fee = fee_for(gross)
            cash -= gross + fee
            units[asset] += units_change
            net_cash = -(gross + fee)
        else:
            gross = min(float(gross_target), units[asset] * price)
            if gross <= 0:
                return 0.0
            units_change = gross / price
            fee = fee_for(gross)
            units[asset] -= units_change
            cash += gross - fee
            net_cash = gross - fee
        trade_cost_total += fee
        trades.append({"date": date, "asset": asset, "side": side, "reason": reason, "units": units_change if side == "buy" else -units_change, "price_cny": price, "gross_value_cny": gross, "fee_cny": fee, "cash_change_cny": net_cash, "mode": mode})
        return gross - fee if side == "sell" else gross

    def buy_to_weight(date: pd.Timestamp, asset: str, weight: float, reason: str) -> None:
        current_nav = nav_at(date)
        price = price_on(date, asset, for_trade=True)
        if price is None or current_nav <= 0:
            return
        desired = current_nav * weight
        current = units[asset] * price
        if desired > current:
            target_cash = current_nav * target_weights_for_state()["cash"]
            execute_order(date, asset, desired - current, "buy", reason, cash_floor=target_cash)

    def target_weights_for_state() -> dict[str, float]:
        """Build the web strategy's target weights for the current sleeve states."""
        target = dict(BASE_WEIGHTS)
        for asset in RISK_ASSETS:
            base = BASE_WEIGHTS[asset]
            if not initialized[asset]:
                target[asset] = 0.0
                target["long_bond"] += base
                continue
            if risk_state[asset]:
                continue
            post_multiplier = 0.0 if mode == "sell_all" else 0.5
            target[asset] = base * post_multiplier
            released = base - target[asset]
            target["long_bond"] += released * (1.0 - released_to_cash_ratio)
            target["cash"] += released * released_to_cash_ratio
        total = sum(target.values())
        return {asset: weight / total for asset, weight in target.items()}

    def rebalance_to_state(date: pd.Timestamp, reason: str) -> None:
        """Move defensive holdings/cash to the web engine's new target on a buy signal."""
        current_nav = nav_at(date)
        if current_nav <= 0:
            return
        target_values = {
            asset: current_nav * weight for asset, weight in target_weights_for_state().items()
        }
        target_cash = target_values["cash"]
        order_assets = list(RISK_ASSETS) + ["long_bond"]
        # Sell overweight ETFs first so a buy signal can be funded even when
        # all released proceeds were previously invested in long bonds.
        for asset in order_assets:
            price = price_on(date, asset, for_trade=True)
            if price is None:
                continue
            current_value = units[asset] * price
            excess = current_value - target_values[asset]
            if excess > 0:
                execute_order(date, asset, excess, "sell", f"{reason}_sell")
        # Then buy underweight risk/defensive ETFs from the released cash.
        for asset in order_assets:
            price = price_on(date, asset, for_trade=True)
            if price is None:
                continue
            current_value = units[asset] * price
            shortfall = target_values[asset] - current_value
            if shortfall > 0:
                execute_order(date, asset, shortfall, "buy", f"{reason}_buy", cash_floor=target_cash)

    def deploy_monthly_contribution(date: pd.Timestamp) -> None:
        """Invest new cash only into sleeves currently marked as on."""
        for asset in RISK_ASSETS:
            if risk_state[asset]:
                buy_to_weight(date, asset, BASE_WEIGHTS[asset], "monthly_contribution")
        released_weight = sum(BASE_WEIGHTS[asset] for asset in RISK_ASSETS if not risk_state[asset])
        long_bond_target = BASE_WEIGHTS["long_bond"] + released_weight * (1.0 - released_to_cash_ratio)
        buy_to_weight(date, "long_bond", long_bond_target, "monthly_contribution")

    def next_asset_date(date: pd.Timestamp, asset: str) -> pd.Timestamp | None:
        future = raw_cny[asset].index[raw_cny[asset].index > date]
        return future[0] if len(future) else None

    # Initial allocation: unavailable risk-asset weights stay in long bonds.
    initial_prices = mark_matrix.loc[calendar[0]]
    missing_weight = sum(BASE_WEIGHTS[a] for a in RISK_ASSETS if pd.isna(initial_prices[a]))
    for asset in RISK_ASSETS:
        if pd.notna(initial_prices[asset]):
            units[asset] = initial_capital * BASE_WEIGHTS[asset] / float(initial_prices[asset])
            initialized[asset] = True
    long_bond_weight = BASE_WEIGHTS["long_bond"] + missing_weight
    units["long_bond"] = initial_capital * long_bond_weight / float(initial_prices["long_bond"])

    for date in calendar:
        date = pd.Timestamp(date)
        trade_cost_before = trade_cost_total
        day_contribution = float(contribution_schedule.get(date, 0.0))
        if day_contribution:
            cash += day_contribution
        # Register ex-date entitlements first; cash is credited on the official
        # payment date (or the next portfolio date when payment falls on a
        # weekend/holiday), even if the position is sold in between.
        for asset, asset_events in events.items():
            for event in asset_events:
                if pd.Timestamp(event["ex_date"]).normalize() != date:
                    continue
                if units[asset] <= 0:
                    continue
                amount = float(event["amount_per_share"])
                if asset == "nasdaq100":
                    fx_date = fx.index[fx.index >= date]
                    fx_value = float(fx.loc[fx_date[0]]) if len(fx_date) else None
                    if fx_value is None:
                        continue
                    amount *= fx_value
                payment_date = pd.to_datetime(event.get("pay_date"), errors="coerce")
                if pd.isna(payment_date):
                    payment_date = date
                future_payment_dates = calendar[calendar >= pd.Timestamp(payment_date).normalize()]
                if len(future_payment_dates) == 0:
                    continue
                cash_date = pd.Timestamp(future_payment_dates[0])
                pending_dividend_cash[cash_date] = pending_dividend_cash.get(cash_date, 0.0) + units[asset] * amount

        day_dividend_cash = pending_dividend_cash.pop(date, 0.0)
        if day_dividend_cash:
            cash += day_dividend_cash
            dividend_cash_total += day_dividend_cash

        # New monthly cash buys at today's close and therefore is not entitled
        # to a distribution whose ex-date is today.
        if day_contribution:
            deploy_monthly_contribution(date)

        for asset in RISK_ASSETS:
            if not initialized[asset] and date in raw_cny[asset].index:
                initialized[asset] = True
                # A newly available ETF replaces its long-bond substitute at the native start date.
                target_value = nav_at(date) * BASE_WEIGHTS[asset]
                long_bond_price = price_on(date, "long_bond", for_trade=True)
                extra_long_bond = max(0.0, units["long_bond"] * long_bond_price - nav_at(date) * BASE_WEIGHTS["long_bond"]) if long_bond_price else 0.0
                if extra_long_bond > 0:
                    execute_order(date, "long_bond", min(extra_long_bond, target_value), "sell", "coverage_start_funding")
                buy_to_weight(date, asset, BASE_WEIGHTS[asset], "coverage_start")

        for asset in list(pending):
            instruction = pending[asset]
            if instruction["date"] != date:
                continue
            pending.pop(asset)
            price = price_on(date, asset, for_trade=True)
            if price is None:
                pending[asset] = instruction
                continue
            if instruction["action"] == "sell":
                current_nav = nav_at(date)
                current_value = units[asset] * price
                weight = current_value / current_nav if current_nav > 0 else 0.0
                target_weight = BASE_WEIGHTS[asset]
                upper_weight = target_weight * (1 + SELL_DRIFT_BAND)
                upper_weight_threshold = upper_weight if SELL_DRIFT_BAND > 0 else np.nan
                if SELL_DRIFT_BAND > 0 and weight <= upper_weight:
                    trades.append({"date": date, "asset": asset, "side": "skip", "reason": "sell_weight_gate", "units": 0.0, "price_cny": price, "gross_value_cny": 0.0, "fee_cny": 0.0, "cash_change_cny": 0.0, "mode": mode, "pre_trade_weight": weight, "target_weight": target_weight, "upper_weight_threshold": upper_weight_threshold, "position_value_cny": current_value, "sell_fraction": 0.0})
                    gate_waiting[asset] = True
                else:
                    post_sell_weight = 0.0 if mode == "sell_all" else target_weight * 0.5
                    gross_to_sell = max(0.0, current_value - current_nav * post_sell_weight)
                    fraction = gross_to_sell / current_value if current_value > 0 else 0.0
                    cash_before_sale = cash
                    sold_net = execute_order(date, asset, gross_to_sell, "sell", "ma250_sell_position")
                    if trades and trades[-1]["asset"] == asset and trades[-1]["side"] == "sell":
                        trades[-1].update({"pre_trade_weight": weight, "target_weight": target_weight, "upper_weight_threshold": upper_weight_threshold, "post_sell_target_weight": post_sell_weight, "position_value_cny": current_value, "sell_fraction": fraction})
                    if sold_net > 0:
                        cash_target = sold_net * released_to_cash_ratio
                        bond_target = sold_net - cash_target
                        # Reinvest only this sale's net proceeds; preserve all
                        # cash that existed before the sale.
                        execute_order(
                            date,
                            "long_bond",
                            bond_target,
                            "buy",
                            "sell_proceeds_to_long_bond",
                            cash_floor=cash_before_sale + cash_target,
                        )
                    gate_waiting[asset] = False
                risk_state[asset] = False
                below[asset] = 0
                above[asset] = 0
            else:
                gate_waiting[asset] = False
                risk_state[asset] = True
                below[asset] = 0
                above[asset] = 0
                rebalance_to_state(date, "ma250_buy")

        # For a drift-gated strategy, a skipped sell remains eligible on later
        # check dates. Once the sleeve is above its threshold, sell down to the
        # configured sell_all/sell_half target in one transaction.
        if date in check_dates and SELL_DRIFT_BAND > 0:
            for asset in RISK_ASSETS:
                if not gate_waiting[asset] or asset in pending:
                    continue
                price = price_on(date, asset, for_trade=True)
                if price is None:
                    continue
                current_nav = nav_at(date)
                current_value = units[asset] * price
                weight = current_value / current_nav if current_nav > 0 else 0.0
                target_weight = BASE_WEIGHTS[asset]
                upper_weight = target_weight * (1 + SELL_DRIFT_BAND)
                if weight <= upper_weight:
                    continue
                post_sell_weight = 0.0 if mode == "sell_all" else target_weight * 0.5
                gross_to_sell = max(0.0, current_value - current_nav * post_sell_weight)
                fraction = gross_to_sell / current_value if current_value > 0 else 0.0
                cash_before_sale = cash
                sold_net = execute_order(date, asset, gross_to_sell, "sell", "sell_weight_gate_release")
                if trades and trades[-1]["asset"] == asset and trades[-1]["side"] == "sell":
                    trades[-1].update({"pre_trade_weight": weight, "target_weight": target_weight, "upper_weight_threshold": upper_weight, "post_sell_target_weight": post_sell_weight, "position_value_cny": current_value, "sell_fraction": fraction})
                if sold_net > 0:
                    cash_target = sold_net * released_to_cash_ratio
                    bond_target = sold_net - cash_target
                    execute_order(
                        date,
                        "long_bond",
                        bond_target,
                        "buy",
                        "sell_proceeds_to_long_bond",
                        cash_floor=cash_before_sale + cash_target,
                    )
                gate_waiting[asset] = False

        if date in check_dates:
            for asset in RISK_ASSETS:
                paused = event_status[asset]["status"] == "warning"
                if paused:
                    continue

                if frequency == "daily":
                    raw_value = raw_cny[asset].get(date)
                    signal = signal_cny[asset].get(date)
                    ma = ma_series[asset].get(date)
                    if pd.isna(raw_value) or pd.isna(signal) or pd.isna(ma):
                        continue
                    signal_value = float(signal)
                    ma_value = float(ma)
                    if risk_state[asset]:
                        below[asset] = below[asset] + 1 if signal_value < ma_value * (1 - BAND) else 0
                        above[asset] = 0
                        if below[asset] >= confirmation_days and asset not in pending:
                            execution_date = next_asset_date(date, asset)
                            if execution_date is not None:
                                pending[asset] = {"action": "sell", "date": execution_date, "signal_date": date}
                            below[asset] = 0
                    else:
                        above[asset] = above[asset] + 1 if signal_value > ma_value * (1 + BAND) else 0
                        below[asset] = 0
                        if above[asset] >= confirmation_days and asset not in pending:
                            execution_date = next_asset_date(date, asset)
                            if execution_date is not None:
                                pending[asset] = {"action": "buy", "date": execution_date, "signal_date": date}
                            above[asset] = 0
                    signal_log.append({"date": date, "asset": asset, "signal_close_cny": signal_value, "ma250_cny": ma_value, "below_count": below[asset], "above_count": above[asset], "state": "on" if risk_state[asset] else "off", "pending_action": pending.get(asset, {}).get("action", ""), "signal_paused": paused, "check_frequency": frequency})
                    continue

                # Monthly modes keep the daily MA250 and configurable own-trading-
                # day confirmation, but only evaluate on their configured monthly
                # checkpoint dates.
                own_dates = raw_cny[asset].index[
                    (raw_cny[asset].index >= start_date) & (raw_cny[asset].index <= date)
                ]
                recent_dates = own_dates[-confirmation_days:]
                if len(recent_dates) < confirmation_days:
                    continue
                recent_signal = signal_cny[asset].reindex(recent_dates)
                recent_ma = ma_series[asset].reindex(recent_dates)
                if recent_signal.isna().any() or recent_ma.isna().any():
                    continue
                sell_condition = bool((recent_signal < recent_ma * (1 - BAND)).all())
                buy_condition = bool((recent_signal > recent_ma * (1 + BAND)).all())
                signal_value = float(recent_signal.iloc[-1])
                ma_value = float(recent_ma.iloc[-1])
                if risk_state[asset]:
                    below[asset] = confirmation_days if sell_condition else 0
                    above[asset] = 0
                    if sell_condition and asset not in pending:
                        execution_date = next_asset_date(date, asset)
                        if execution_date is not None:
                            pending[asset] = {"action": "sell", "date": execution_date, "signal_date": date}
                        below[asset] = 0
                else:
                    above[asset] = confirmation_days if buy_condition else 0
                    below[asset] = 0
                    if buy_condition and asset not in pending:
                        execution_date = next_asset_date(date, asset)
                        if execution_date is not None:
                            pending[asset] = {"action": "buy", "date": execution_date, "signal_date": date}
                        above[asset] = 0
                signal_log.append({"date": date, "asset": asset, "signal_close_cny": signal_value, "ma250_cny": ma_value, "below_count": below[asset], "above_count": above[asset], "state": "on" if risk_state[asset] else "off", "pending_action": pending.get(asset, {}).get("action", ""), "signal_paused": paused, "check_frequency": frequency, "confirmation_dates": ",".join(pd.Timestamp(value).strftime("%Y-%m-%d") for value in recent_dates)})

        nav = nav_at(date)
        row = {
            "date": date,
            "nav_cny": nav,
            "cash_cny": cash,
            "contribution_cny": day_contribution,
            "dividend_cash_cny": day_dividend_cash,
            "trade_cost_cny": trade_cost_total - trade_cost_before,
        }
        total_asset_value = 0.0
        for asset in ALL_ASSETS:
            value = units[asset] * (price_on(date, asset) or 0.0)
            row[f"{asset}_value_cny"] = value
            row[f"{asset}_weight"] = value / nav if nav else 0.0
            total_asset_value += value
        row["invested_value_cny"] = total_asset_value
        nav_rows.append(row)

    nav_frame = add_flow_adjusted_metrics(pd.DataFrame(nav_rows), initial_capital)
    nav_frame["peak_nav_cny"] = nav_frame["nav_cny"].cummax()
    nav_frame["peak_twr_value_cny"] = nav_frame["twr_peak_index"] * initial_capital
    trade_frame = pd.DataFrame(trades)
    signal_frame = pd.DataFrame(signal_log)
    results_dir = output_root / "results"
    result_prefix = {"daily": "daily", "monthly": "monthly", "monthly_10th": "monthly_10th"}[frequency]
    # The raw-signal scenario still credits verified distributions in the
    # final NAV; the suffix describes only the signal input.
    basis_suffix = "" if signal_basis == "total_return" else "_raw_signal"
    contribution_suffix = f"_monthly_{monthly_contribution:g}" if monthly_contribution else "_no_contribution"
    start_suffix = f"_from_{start_date:%Y%m%d}"
    result_suffix = basis_suffix + contribution_suffix + start_suffix
    trade_prefix = "trade_log" if frequency == "daily" else "monthly_trade_log"
    signal_prefix = "signals" if frequency == "daily" else "monthly_signals"
    annual_frame = annual_metrics_from_nav(nav_frame, initial_capital)
    annual_frame.insert(0, "mode", mode)
    annual_frame.insert(1, "frequency", frequency)
    annual_frame.insert(2, "signal_basis", signal_basis)
    annual_frame.insert(3, "monthly_contribution_cny", monthly_contribution)
    annual_frame.insert(4, "strategy_start_date", str(start_date.date()))
    write_frame(results_dir / f"{result_prefix}{result_suffix}_nav_{mode}.csv", nav_frame)
    write_frame(results_dir / f"{trade_prefix}{result_suffix}_{mode}.csv", trade_frame if not trade_frame.empty else pd.DataFrame(columns=["date", "asset", "side", "reason"]))
    write_frame(results_dir / f"{signal_prefix}{result_suffix}_{mode}.csv", signal_frame if not signal_frame.empty else pd.DataFrame(columns=["date", "asset"]))
    write_frame(results_dir / f"annual_{result_prefix}{result_suffix}_{mode}.csv", annual_frame)
    elapsed_years = max((nav_frame["date"].iloc[-1] - nav_frame["date"].iloc[0]).days / 365.25, 1 / 365.25)
    total_return = float(nav_frame["twr_index"].iloc[-1] - 1.0)
    cagr = float((1.0 + total_return) ** (1.0 / elapsed_years) - 1.0)
    min_dd_index = nav_frame["drawdown"].idxmin()
    max_dd = float(nav_frame.loc[min_dd_index, "drawdown"])
    recovery = drawdown_recovery_metrics(nav_frame["twr_index"], nav_frame["date"])
    summary = {
        "mode": mode,
        "frequency": frequency,
        "signal_basis": signal_basis,
        "signal_dividends_used": signal_basis == "total_return",
        "cash_dividends_used": True,
        "start_date": str(nav_frame["date"].iloc[0].date()),
        "end_date": str(nav_frame["date"].iloc[-1].date()),
        "initial_capital_cny": initial_capital,
        "final_nav_cny": float(nav_frame["nav_cny"].iloc[-1]),
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "max_drawdown_date": str(nav_frame.loc[min_dd_index, "date"].date()),
        "max_drawdown_peak_date": recovery["peak_date"],
        "max_drawdown_recovery_date": recovery["recovery_date"],
        "max_drawdown_recovery_days": recovery["recovery_days"],
        "trading_days": int(len(nav_frame)),
        "signal_check_dates": int(len(check_dates)),
        "trade_rows": int(len(trade_frame)),
        "commission_total_cny": float(trade_cost_total),
        "dividend_cash_total_cny": float(dividend_cash_total),
        "monthly_contribution_cny": float(monthly_contribution),
        "strategy_start_date": str(start_date.date()),
        "contribution_days": int(sum(bool(value) for value in contribution_schedule.values())),
        "total_contributions_cny": float(nav_frame["contribution_cny"].sum()),
        "total_capital_added_cny": float(initial_capital + nav_frame["contribution_cny"].sum()),
        "net_profit_after_contributions_cny": float(nav_frame["nav_cny"].iloc[-1] - initial_capital - nav_frame["contribution_cny"].sum()),
        "twr_total_return": total_return,
        "account_value_return_vs_initial": float(nav_frame["nav_cny"].iloc[-1] / initial_capital - 1.0),
        "annual_metrics_file": str(results_dir / f"annual_{result_prefix}{result_suffix}_{mode}.csv"),
        "ma_days": MA_DAYS,
        "ma_band": BAND,
        "confirmation_days": confirmation_days,
        "sell_drift_band": SELL_DRIFT_BAND,
        "released_to_cash_ratio": released_to_cash_ratio,
        "base_weights": BASE_WEIGHTS,
        "paused_assets": [asset for asset in RISK_ASSETS if event_status[asset]["status"] == "warning"],
        "cash_dividend_warning_assets": [
            asset for asset in ALL_ASSETS if data["event_status"].get(asset, {}).get("status") == "warning"
        ],
        "asset_coverage_warning_assets": [
            asset for asset in ALL_ASSETS if raw_cny[asset].index[0] > start_date
        ],
        "data_quality": (
            "warning"
            if any(value.get("status") == "warning" for value in data["event_status"].values())
            else "raw_signal_total_return_cash" if signal_basis == "raw_price" else "verified"
        ),
    }
    return nav_frame, trade_frame, summary


def asset_return_table(data: dict[str, Any], output_root: Path, signal_basis: str = DEFAULT_SIGNAL_BASIS) -> pd.DataFrame:
    if signal_basis not in SIGNAL_BASES:
        raise ValueError(f"Unsupported signal basis: {signal_basis}")
    rows: list[dict[str, Any]] = []
    for asset in ALL_ASSETS:
        if signal_basis == "raw_price":
            series = data["raw_official"]["long_bond"] if asset == "long_bond" else data["raw_cny"][asset]
            status = "verified_raw_price_only"
        else:
            series = data["signal_cny"][asset]
            if asset == "long_bond":
                official = data["raw_official"]["long_bond"]
                events = data["events"][asset]
                series, _ = apply_dividends(official, events)
            status = data["event_status"][asset]["status"]
        native_start = series.index[0]
        elapsed = max((series.index[-1] - native_start).days / 365.25, 1 / 365.25)
        price_return = float(series.iloc[-1] / series.iloc[0] - 1.0)
        drawdown = series / series.cummax() - 1.0
        rows.append({
            "asset": asset,
            "native_start": native_start.date(),
            "native_end": series.index[-1].date(),
            "return_basis": "raw_price_only" if signal_basis == "raw_price" else "total_return_adjusted",
            "data_quality": status,
            "period_return": price_return,
            "total_return": price_return if signal_basis == "total_return" and status in {"verified", "verified_no_distribution"} else np.nan,
            "price_return": price_return if signal_basis == "raw_price" else np.nan,
            "price_return_provisional": price_return if signal_basis == "total_return" and status == "warning" else np.nan,
            "cagr": (1 + price_return) ** (1 / elapsed) - 1,
            "max_drawdown": float(drawdown.min()),
        })
    frame = pd.DataFrame(rows)
    output_name = "asset_returns.csv" if signal_basis == "total_return" else "asset_returns_raw_price.csv"
    write_frame(output_root / "results" / output_name, frame)
    return frame


def frequency_comparison(summaries: list[dict[str, Any]]) -> pd.DataFrame:
    by_key = {(item["frequency"], item["mode"]): item for item in summaries}
    rows: list[dict[str, Any]] = []
    for mode in ("sell_all", "sell_half"):
        daily = by_key.get(("daily", mode))
        monthly = by_key.get(("monthly", mode))
        if daily is None or monthly is None:
            continue
        rows.append(
            {
                "mode": mode,
                "signal_basis": daily["signal_basis"],
                "start_date": daily["start_date"],
                "end_date": daily["end_date"],
                "daily_final_nav_cny": daily["final_nav_cny"],
                "monthly_final_nav_cny": monthly["final_nav_cny"],
                "monthly_minus_daily_final_nav_cny": monthly["final_nav_cny"] - daily["final_nav_cny"],
                "daily_total_return": daily["total_return"],
                "monthly_total_return": monthly["total_return"],
                "monthly_minus_daily_total_return": monthly["total_return"] - daily["total_return"],
                "daily_cagr": daily["cagr"],
                "monthly_cagr": monthly["cagr"],
                "monthly_minus_daily_cagr": monthly["cagr"] - daily["cagr"],
                "daily_max_drawdown": daily["max_drawdown"],
                "monthly_max_drawdown": monthly["max_drawdown"],
                "monthly_minus_daily_max_drawdown": monthly["max_drawdown"] - daily["max_drawdown"],
                "daily_trade_rows": daily["trade_rows"],
                "monthly_trade_rows": monthly["trade_rows"],
                "monthly_contribution_cny": daily["monthly_contribution_cny"],
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily/monthly MA250 strategy comparison using one official-data snapshot.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--initial-capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--frequency", choices=("daily", "monthly", "monthly_10th", "both"), default="both")
    parser.add_argument("--signal-basis", choices=SIGNAL_BASES, default=DEFAULT_SIGNAL_BASIS)
    parser.add_argument("--monthly-contribution", type=float, default=DEFAULT_MONTHLY_CONTRIBUTION)
    parser.add_argument("--start-date", default=START_DATE.strftime("%Y-%m-%d"))
    parser.add_argument("--low-vol-source", choices=LOW_VOL_SOURCES, default=DEFAULT_LOW_VOL_SOURCE)
    parser.add_argument("--confirmation-days", type=int, default=DEFAULT_CONFIRMATION_DAYS)
    parser.add_argument("--released-to-cash-ratio", type=float, default=RELEASED_TO_CASH_RATIO, help="Fraction of released risk-asset proceeds left as cash; the remainder buys long bonds.")
    args = parser.parse_args()
    if args.monthly_contribution < 0:
        parser.error("--monthly-contribution cannot be negative")
    if args.confirmation_days < 1:
        parser.error("--confirmation-days must be at least 1")
    if not 0.0 <= args.released_to_cash_ratio <= 1.0:
        parser.error("--released-to-cash-ratio must be between 0 and 1")
    try:
        strategy_start = pd.Timestamp(args.start_date).normalize()
    except Exception as exc:
        parser.error(f"--start-date is invalid: {exc}")
    data = prepare_data(args.output_dir, strategy_start, args.low_vol_source)
    # Asset-level performance is a return report, so it always uses the
    # dividend-adjusted series even when signals use raw prices.
    asset_frame = asset_return_table(data, args.output_dir, "total_return")
    frequencies = CHECK_FREQUENCIES if args.frequency == "both" else (args.frequency,)
    summaries: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    for frequency in frequencies:
        for mode in ("sell_all", "sell_half"):
            nav_frame, _, summary = run_backtest(
                data,
                mode,
                args.output_dir,
                args.initial_capital,
                frequency=frequency,
                signal_basis=args.signal_basis,
                monthly_contribution=args.monthly_contribution,
                start_date=strategy_start,
                confirmation_days=args.confirmation_days,
                released_to_cash_ratio=args.released_to_cash_ratio,
            )
            summaries.append(summary)
            annual_frame = annual_metrics_from_nav(nav_frame, args.initial_capital)
            annual_frame.insert(0, "mode", mode)
            annual_frame.insert(1, "frequency", frequency)
            annual_frame.insert(2, "signal_basis", args.signal_basis)
            annual_frame.insert(3, "monthly_contribution_cny", args.monthly_contribution)
            annual_frame.insert(4, "strategy_start_date", str(strategy_start.date()))
            annual_frames.append(annual_frame)
    summary_frame = pd.DataFrame(summaries)
    basis_suffix = "" if args.signal_basis == "total_return" else "_raw_signal"
    contribution_suffix = f"_monthly_{args.monthly_contribution:g}" if args.monthly_contribution else "_no_contribution"
    start_suffix = f"_from_{strategy_start:%Y%m%d}"
    result_suffix = basis_suffix + contribution_suffix + start_suffix
    write_frame(args.output_dir / "results" / f"strategy_summary{result_suffix}.csv", summary_frame)
    comparison_frame = frequency_comparison(summaries)
    write_frame(args.output_dir / "results" / f"frequency_comparison{result_suffix}.csv", comparison_frame)
    annual_strategy_frame = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    write_frame(args.output_dir / "results" / f"annual_strategy{result_suffix}.csv", annual_strategy_frame)
    if args.frequency == "monthly_10th":
        confirmation_rule = f"{args.confirmation_days} consecutive own trading day(s) below/above the configured band; evaluate on the completed trading day immediately before the first trading day on/after the 10th, then execute on that next trading day"
    elif args.frequency == "monthly":
        confirmation_rule = f"{args.confirmation_days} consecutive own trading day(s) below/above the configured band; evaluate at each month-end checkpoint"
    elif args.frequency == "daily":
        confirmation_rule = f"{args.confirmation_days} consecutive own trading day(s) below/above the configured band; execute on the next trading day"
    else:
        confirmation_rule = f"{args.confirmation_days} consecutive own trading day(s) below/above the configured band; daily, month-end, and 10th-of-month checkpoints are included"
    write_json(
        args.output_dir / "results" / f"run_summary{result_suffix}.json",
        {
            "strategy": summaries,
            "frequency_comparison": comparison_frame.to_dict(orient="records"),
            "comparison_basis": {
                "same_prepared_data_snapshot": True,
                "same_start_date": str(strategy_start.date()),
                "signal_basis": args.signal_basis,
                "signal_dividends_used": args.signal_basis == "total_return",
                "return_basis": "total_return_adjusted",
                "cash_dividends_used": True,
                "cash_dividend_warning_assets": [
                    asset for asset in ALL_ASSETS if data["event_status"].get(asset, {}).get("status") == "warning"
                ],
                "monthly_contribution_cny": args.monthly_contribution,
                "start_date": str(strategy_start.date()),
                "coverage_policy": "QQQ uses Yahoo third-party history from 2005-01-03; other assets without native data at the selected start remain in the long-bond sleeve until their native start",
                "contribution_timing": "first available portfolio trading day of each calendar month, before that day's strategy orders",
                "return_method": "time-weighted return; external monthly contributions excluded from annual return and drawdown",
                "ma_days": MA_DAYS,
                "ma_band": BAND,
                "confirmation_days": args.confirmation_days,
                "confirmation_rule": confirmation_rule,
                "sell_drift_band": SELL_DRIFT_BAND,
                "released_to_cash_ratio": args.released_to_cash_ratio,
                "low_vol_source": args.low_vol_source,
                "commission_rate": COMMISSION_RATE,
                "minimum_commission": MINIMUM_COMMISSION,
                "base_weights": BASE_WEIGHTS,
            },
            "asset_returns": asset_frame.to_dict(orient="records"),
            "annual_strategy": annual_strategy_frame.to_dict(orient="records"),
            "source_manifest": data["manifest"],
        },
    )
    print(summary_frame.to_string(index=False))
    print("frequency_comparison")
    print(comparison_frame.to_string(index=False))
    print("asset_returns")
    print(asset_frame.to_string(index=False))


if __name__ == "__main__":
    # Allow execution from the project root without installing the parent app as a package.
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    main()
