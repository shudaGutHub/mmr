"""Alpaca-first universe screen: intraday ATR% x dollar volume, biotech out."""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml

ET = ZoneInfo("America/New_York")
UNIVERSE_PATH = Path(r"c:\Users\salee\BITBUCKET\gapreversion211\universe.txt")
CACHE = Path(r"c:\Users\salee\BITBUCKET\mmr\data\factory_cache")
OUT_PATH = Path(
    r"C:\Users\salee\.cursor\projects\c-Users-salee-BITBUCKET-mmr\canvases"
    r"\symbol-agent-factory.screen.json"
)
WORKSPACE_OUT = Path(r"c:\Users\salee\BITBUCKET\mmr\data\symbol_agent_factory_screen.json")
DATA_URL = "https://data.alpaca.markets"

BIOTECH_NEEDLES = (
    "biotech",
    "biotechnology",
    "biological product",
    "biological products",
    "biologicals",
)


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def merge_envs() -> dict[str, str]:
    merged: dict[str, str] = {}
    for p in (
        Path(r"c:\Users\salee\BITBUCKET\flateod-lite\.env"),
        Path(r"c:\Users\salee\BITBUCKET\gaphold17\.env"),
        Path(r"c:\Users\salee\BITBUCKET\databento-relay\databento-relay\.env"),
        Path(r"c:\Users\salee\BITBUCKET\HandsOnAITradingBook\.env"),
        Path.home() / ".config" / "mmr" / "secrets.env",
    ):
        merged.update(load_env_file(p))
    return merged


def alpaca_headers(env: dict[str, str]) -> dict[str, str]:
    pairs = [
        ("ALPACA_FLAT_NONEWS_KEY", "ALPACA_FLAT_NONEWS_SECRET"),
        ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
        ("ALPACA_API_KEY", "ALPACA_API_SECRET"),
        ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
        ("ALPACA_KAOS_FLAT_1M_KEY", "ALPACA_KAOS_FLAT_1M_SECRET"),
        ("ALPACA_PORT_EOD_KEY", "ALPACA_PORT_EOD_SECRET"),
    ]
    for k, s in pairs:
        if env.get(k) and env.get(s):
            print(f"alpaca_key_source={k}", flush=True)
            return {"APCA-API-KEY-ID": env[k], "APCA-API-SECRET-KEY": env[s]}
    raise RuntimeError("no Alpaca key pair found in env files")


def load_symbols(path: Path) -> list[str]:
    symbols, seen = [], set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sym = line.split()[0].upper()
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def _f(val) -> float | None:
    if val is None:
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def percentile_ranks(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        pct = (avg + 1.0) / n
        for k in range(i, j + 1):
            ranks[order[k]] = pct
        i = j + 1
    return ranks


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        if len(close) < 2:
            return None
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1).dropna()
        return float(tr.mean()) if len(tr) else None
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    val = atr.iloc[-1]
    return float(val) if pd.notna(val) else None


def alpaca_get(headers: dict, path: str, params: dict, feed_fallback: str) -> tuple[dict, str]:
    feeds = [params.get("feed") or "sip", "iex"]
    seen = []
    last_err = None
    for feed in feeds:
        if feed in seen:
            continue
        seen.append(feed)
        q = dict(params)
        q["feed"] = feed
        resp = requests.get(f"{DATA_URL}{path}", headers=headers, params=q, timeout=45)
        if resp.status_code in (401, 403) and feed == "sip":
            print(f"sip not entitled on {path}; falling back to iex", flush=True)
            continue
        if resp.status_code == 429:
            time.sleep(3)
            resp = requests.get(f"{DATA_URL}{path}", headers=headers, params=q, timeout=45)
        resp.raise_for_status()
        return resp.json(), feed
    raise RuntimeError(f"alpaca {path} failed: {last_err}")


def fetch_snapshots(headers: dict, symbols: list[str], feed: str) -> tuple[dict, str]:
    used = feed
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 40):
        chunk = symbols[i : i + 40]
        payload, used = alpaca_get(
            headers,
            "/v2/stocks/snapshots",
            {"symbols": ",".join(chunk), "feed": used},
            used,
        )
        if isinstance(payload, dict):
            for sym, snap in payload.items():
                if isinstance(snap, dict):
                    out[sym.upper()] = snap
        print(f"  snapshots {min(i+40, len(symbols))}/{len(symbols)} feed={used}", flush=True)
        time.sleep(0.15)
    return out, used


def fetch_bars(headers: dict, symbols: list[str], feed: str, timeframe: str, start_iso: str, end_iso: str) -> dict[str, list]:
    out: dict[str, list] = {s: [] for s in symbols}
    used = feed
    for i in range(0, len(symbols), 25):
        chunk = symbols[i : i + 25]
        page_token = None
        while True:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start_iso,
                "end": end_iso,
                "limit": 10000,
                "adjustment": "raw",
                "feed": used,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            payload, used = alpaca_get(headers, "/v2/stocks/bars", params, used)
            bars = payload.get("bars") or {}
            if isinstance(bars, dict):
                for sym, rows in bars.items():
                    out.setdefault(sym.upper(), []).extend(rows or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        print(f"  bars {min(i+25, len(symbols))}/{len(symbols)} tf={timeframe} feed={used}", flush=True)
        time.sleep(0.2)
    return out


def session_metrics_from_snap(sym: str, snap: dict) -> dict | None:
    daily = snap.get("dailyBar") or snap.get("daily_bar") or {}
    prev = snap.get("prevDailyBar") or snap.get("prev_daily_bar") or {}
    latest = snap.get("latestTrade") or snap.get("latest_trade") or {}
    minute = snap.get("minuteBar") or snap.get("minute_bar") or {}
    last = _f(latest.get("p") or latest.get("price")) or _f(minute.get("c")) or _f(daily.get("c"))
    high = _f(daily.get("h"))
    low = _f(daily.get("l"))
    volume = _f(daily.get("v")) or 0.0
    vwap = _f(daily.get("vw"))
    day_open = _f(daily.get("o"))
    prev_close = _f(prev.get("c"))
    if not last or last <= 0 or high is None or low is None:
        return None
    tr_parts = [high - low]
    if prev_close and prev_close > 0:
        tr_parts.append(abs(high - prev_close))
        tr_parts.append(abs(low - prev_close))
    session_tr = max(tr_parts)
    dollar_volume = (vwap * volume) if vwap and vwap > 0 and volume > 0 else last * volume
    gap_pct = ((day_open - prev_close) / prev_close) if day_open and prev_close and prev_close > 0 else None
    change_pct = ((last - prev_close) / prev_close) if prev_close and prev_close > 0 else None
    return {
        "symbol": sym,
        "last": last,
        "high": high,
        "low": low,
        "open": day_open,
        "prev_close": prev_close,
        "volume": volume,
        "vwap": vwap,
        "session_tr_pct": session_tr / last,
        "dollar_volume": dollar_volume,
        "gap_pct": gap_pct,
        "change_pct": change_pct,
    }


def atr_from_bars(rows: list[dict], last: float) -> tuple[float | None, int]:
    if not rows:
        return None, 0
    df = pd.DataFrame(rows)
    if df.empty or "h" not in df:
        return None, 0
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
    df = df.set_index("t").sort_index()
    # RTH only
    idx = df.index
    minutes = idx.hour * 60 + idx.minute
    df = df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]
    if df.empty:
        return None, 0
    atr = wilder_atr(df["h"].astype(float), df["l"].astype(float), df["c"].astype(float), 14)
    if atr is None or last <= 0:
        return None, len(df)
    return atr / last, len(df)


def is_biotech(industry: str, sector: str, sic: str, sic_desc: str, name: str) -> tuple[bool, str]:
    sic_n = str(sic or "").strip()
    blob = " ".join([industry or "", sector or "", sic_desc or "", name or ""]).lower()
    if sic_n in {"2836"} or sic_n.lstrip("0") == "2836":
        return True, f"SIC {sic_n} Biological Products"
    for needle in BIOTECH_NEEDLES:
        if needle in blob:
            loc = "Industry" if needle in (industry or "").lower() else (
                "Sector" if needle in (sector or "").lower() else "sic/name"
            )
            return True, f"{loc} match: {needle}"
    return False, ""


def fetch_fmp_profiles(api_key: str, symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not api_key:
        return out
    for i in range(0, len(symbols), 50):
        chunk = symbols[i : i + 50]
        url = f"https://financialmodelingprep.com/api/v3/profile/{','.join(chunk)}"
        try:
            resp = requests.get(url, params={"apikey": api_key}, timeout=40)
            if resp.status_code == 429:
                time.sleep(5)
                resp = requests.get(url, params={"apikey": api_key}, timeout=40)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                for row in data:
                    sym = str(row.get("symbol") or "").upper()
                    if sym:
                        out[sym] = {
                            "name": row.get("companyName") or "",
                            "industry": row.get("industry") or "",
                            "sector": row.get("sector") or "",
                            "source": "fmp_profile",
                        }
            print(f"  fmp profiles {min(i+50, len(symbols))}/{len(symbols)} got={len(out)}", flush=True)
        except Exception as exc:
            print(f"  fmp batch failed: {type(exc).__name__}", flush=True)
        time.sleep(0.25)
    return out


def fetch_av_overview(api_key: str, symbol: str) -> dict:
    cache = CACHE / "av_overview" / f"{symbol}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "OVERVIEW", "symbol": symbol, "apikey": api_key},
            timeout=30,
        )
        data = resp.json() if resp.ok else {}
        if not isinstance(data, dict) or "Note" in data or "Information" in data:
            return {"symbol": symbol, "error": "av_rate_or_empty"}
        out = {
            "symbol": symbol,
            "name": data.get("Name") or "",
            "industry": data.get("Industry") or "",
            "sector": data.get("Sector") or "",
            "source": "alphavantage_overview",
        }
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
        return out
    except Exception as exc:
        return {"symbol": symbol, "error": type(exc).__name__}


def load_massive_details() -> dict[str, dict]:
    ddir = CACHE / "details"
    out = {}
    if not ddir.exists():
        return out
    for p in ddir.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            out[str(rec.get("symbol") or p.stem).upper()] = rec
        except Exception:
            continue
    return out


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    symbols = load_symbols(UNIVERSE_PATH)
    env = merge_envs()
    headers = alpaca_headers(env)
    av_key = env.get("ALPHAVANTAGE_API_KEY") or env.get("ALPHA_VANTAGE_API_KEY") or ""
    fmp_key = env.get("FMP_API_KEY") or ""
    print(f"universe={len(symbols)} av_key={'yes' if av_key else 'no'} fmp_key={'yes' if fmp_key else 'no'}", flush=True)

    now = datetime.now(tz=ET)
    session_date = now.strftime("%Y-%m-%d")
    start_iso = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ET).astimezone(
        ZoneInfo("UTC")
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("fetching alpaca snapshots...", flush=True)
    snaps, feed = fetch_snapshots(headers, symbols, "sip")
    print(f"snapshots n={len(snaps)} feed={feed}", flush=True)

    print("fetching alpaca 5Min RTH bars for ATR(14)...", flush=True)
    bars = fetch_bars(headers, symbols, feed, "5Min", start_iso, end_iso)

    by_sym: dict[str, dict] = {}
    missing: list[str] = []
    for sym in symbols:
        snap = snaps.get(sym)
        if not snap:
            missing.append(sym)
            continue
        row = session_metrics_from_snap(sym, snap)
        if row is None:
            missing.append(sym)
            continue
        atr_pct, n_bars = atr_from_bars(bars.get(sym) or [], row["last"])
        # Prefer ATR(14) on 5-min RTH; fall back to session TR% if thin bars
        if atr_pct is not None:
            row["atr_pct"] = atr_pct
            row["atr_method"] = "alpaca_5min_wilder_atr14"
        else:
            row["atr_pct"] = row["session_tr_pct"]
            row["atr_method"] = "alpaca_session_tr_pct_fallback"
        row["atr_bars"] = n_bars
        row["dollar_volume_source"] = f"alpaca_{feed}_dailyBar_vwap_x_volume"
        row["atr_source"] = f"alpaca_{feed}_{row['atr_method']}"
        row["price_source"] = f"alpaca_{feed}_snapshot"
        row["session_date"] = session_date
        row["feed"] = feed
        by_sym[sym] = row

    print(f"valid={len(by_sym)} missing={len(missing)}", flush=True)
    if missing:
        print("missing:", " ".join(missing[:40]), flush=True)

    print("fetching industry (FMP batch, AV fill)...", flush=True)
    profiles = fetch_fmp_profiles(fmp_key, list(by_sym))
    massive_details = load_massive_details()
    industry_source_counts = {"fmp_profile": 0, "alphavantage_overview": 0, "massive_sic": 0, "none": 0}

    # AV fill for names missing industry, plus known likely-biotech if FMP says healthcare
    need_av = []
    for sym in by_sym:
        p = profiles.get(sym, {})
        if not p.get("industry"):
            need_av.append(sym)
        else:
            industry_source_counts["fmp_profile"] += 1
    print(f"fmp hit={len(by_sym)-len(need_av)} need_av={len(need_av)}", flush=True)

    av_got = 0
    if av_key:
        for i, sym in enumerate(need_av):
            ov = fetch_av_overview(av_key, sym)
            if ov.get("industry") or ov.get("sector"):
                profiles[sym] = ov
                av_got += 1
                industry_source_counts["alphavantage_overview"] += 1
            if (i + 1) % 20 == 0:
                print(f"  av overview {i+1}/{len(need_av)}", flush=True)
            time.sleep(0.35)
    print(f"av filled={av_got}", flush=True)

    ranked_pool: list[dict] = []
    biotech_dropped: list[dict] = []
    for sym, row in by_sym.items():
        p = profiles.get(sym, {})
        md = massive_details.get(sym, {})
        industry = p.get("industry") or ""
        sector = p.get("sector") or ""
        name = p.get("name") or md.get("name") or ""
        sic = str(md.get("sic") or "")
        sic_desc = str(md.get("sic_description") or "")
        src = p.get("source") or ("massive_sic" if sic else "none")
        if src == "massive_sic":
            industry_source_counts["massive_sic"] += 1
        elif src == "none":
            industry_source_counts["none"] += 1
        rec = {
            **row,
            "name": name,
            "industry": industry or sic_desc,
            "sector": sector,
            "sic": sic,
            "sic_description": sic_desc,
            "industry_source": src,
            "excluded_reason": None,
        }
        bio, reason = is_biotech(industry, sector, sic, sic_desc, name)
        if bio:
            rec["excluded_reason"] = reason
            biotech_dropped.append(rec)
        else:
            ranked_pool.append(rec)

    atrs = [r["atr_pct"] for r in ranked_pool]
    dvols = [r["dollar_volume"] for r in ranked_pool]
    atr_ranks = percentile_ranks(atrs)
    dvol_ranks = percentile_ranks(dvols)
    for rec, ar, dr in zip(ranked_pool, atr_ranks, dvol_ranks):
        rec["atr_pct_rank"] = ar
        rec["dollar_volume_rank"] = dr
        rec["combo_score"] = 0.5 * ar + 0.5 * dr

    ranked_pool.sort(key=lambda r: r["combo_score"], reverse=True)
    keep_n = max(1, math.ceil(len(ranked_pool) * 0.25)) if ranked_pool else 0
    survivors = ranked_pool[:keep_n]
    below_cut = ranked_pool[keep_n:]
    for rec in below_cut:
        rec["excluded_reason"] = "below top 25% combo"

    as_of = datetime.now(tz=ET).isoformat(timespec="seconds")
    payload = {
        "as_of": as_of,
        "screen": {
            "definition": (
                "top 25% of 0.5*pct_rank(intraday ATR%) + 0.5*pct_rank(dollar volume); "
                "biotech excluded before ranking (AV/FMP Industry/Sector + SIC 2836)"
            ),
            "atr_source": f"Alpaca {feed} 5-min RTH Wilder ATR(14) / last; session TR% fallback if <15 bars",
            "dollar_volume_source": f"Alpaca {feed} dailyBar vwap * volume (today RTH developing)",
            "industry_source": "FMP company profile (batch), Alpha Vantage OVERVIEW fill, Massive SIC cache",
            "feed": feed,
            "session_date": session_date,
            "universe_n": len(symbols),
            "valid_snapshots": len(by_sym),
            "missing_snapshot": len(missing),
            "biotech_dropped": len(biotech_dropped),
            "ranked_pool": len(ranked_pool),
            "survivors": len(survivors),
            "industry_source_counts": industry_source_counts,
        },
        "survivors": survivors,
        "biotech_dropped": [
            {
                "symbol": r["symbol"],
                "name": r.get("name"),
                "industry": r.get("industry"),
                "sector": r.get("sector"),
                "sic": r.get("sic"),
                "excluded_reason": r["excluded_reason"],
            }
            for r in sorted(biotech_dropped, key=lambda x: x["symbol"])
        ],
        "below_cut": [
            {"symbol": r["symbol"], "combo_score": r["combo_score"], "excluded_reason": r["excluded_reason"]}
            for r in below_cut
        ],
        "missing": missing,
        "counts": {
            "universe": len(symbols),
            "valid_snapshots": len(by_sym),
            "after_biotech_drop": len(ranked_pool),
            "top_25_agents": len(survivors),
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_OUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2)
    OUT_PATH.write_text(text, encoding="utf-8")
    WORKSPACE_OUT.write_text(text, encoding="utf-8")
    print(json.dumps(payload["counts"], indent=2))
    print("feed", feed, "session", session_date)
    print("survivors:", " ".join(r["symbol"] for r in survivors))
    print("biotech:", " ".join(r["symbol"] for r in sorted(biotech_dropped, key=lambda x: x["symbol"])))
    print("wrote", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
