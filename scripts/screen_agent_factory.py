"""Screen NASDAQ universe: top 25% ATR% x dollar-volume, exclude biotech."""
from __future__ import annotations

import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml
from massive import RESTClient

UNIVERSE_PATH = Path(r"c:\Users\salee\BITBUCKET\gapreversion211\universe.txt")
CONFIG_PATH = Path.home() / ".config" / "mmr" / "trader.yaml"
CACHE_DIR = Path(r"c:\Users\salee\BITBUCKET\mmr\data\factory_cache")
OUT_PATH = Path(
    r"C:\Users\salee\.cursor\projects\c-Users-salee-BITBUCKET-mmr\canvases"
    r"\symbol-agent-factory.screen.json"
)
WORKSPACE_OUT = Path(r"c:\Users\salee\BITBUCKET\mmr\data\symbol_agent_factory_screen.json")

BIOTECH_SIC = {"2836"}
BIOTECH_NEEDLES = (
    "biotech",
    "biological product",
    "biological products",
    "biologicals",
)
SESSION_DATE = "2026-09-04"
PREV_DATE = "2026-09-03"


def load_symbols(path: Path) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
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


def is_biotech(details: dict) -> tuple[bool, str]:
    sic = str(details.get("sic") or details.get("sic_code") or "").strip()
    sic_desc = str(details.get("sic_description") or "").lower()
    industry = str(details.get("industry") or "").lower()
    name = str(details.get("name") or "").lower()
    blob = " ".join([sic_desc, industry, name])
    if sic in BIOTECH_SIC or sic.lstrip("0") == "2836":
        return True, f"SIC {sic} Biological Products"
    for needle in BIOTECH_NEEDLES:
        if needle in blob:
            return True, f"industry/sic match: {needle}"
    return False, ""


def agg_to_dict(a) -> dict:
    return {
        "ticker": (getattr(a, "ticker", None) or "").upper(),
        "open": _f(getattr(a, "open", None)),
        "high": _f(getattr(a, "high", None)),
        "low": _f(getattr(a, "low", None)),
        "close": _f(getattr(a, "close", None)),
        "volume": _f(getattr(a, "volume", None)),
        "vwap": _f(getattr(a, "vwap", None)),
        "timestamp": getattr(a, "timestamp", None),
    }


def fetch_grouped(client: RESTClient, date: str) -> dict[str, dict]:
    cache = CACHE_DIR / f"grouped_{date}.json"
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
        print(f"cache hit {date} n={len(rows)}", flush=True)
        return {r["ticker"]: r for r in rows if r.get("ticker")}
    last_err = None
    for attempt in range(6):
        try:
            aggs = list(client.get_grouped_daily_aggs(date, adjusted=True))
            rows = [agg_to_dict(a) for a in aggs]
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows), encoding="utf-8")
            print(f"fetched {date} n={len(rows)}", flush=True)
            return {r["ticker"]: r for r in rows if r.get("ticker")}
        except Exception as exc:
            last_err = exc
            wait = 8 * (attempt + 1)
            print(f"grouped {date} attempt {attempt+1} failed: {type(exc).__name__}; sleep {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"grouped {date} failed: {last_err}")


def fetch_details(client: RESTClient, ticker: str) -> dict:
    cache = CACHE_DIR / "details" / f"{ticker}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    last_err = ""
    for attempt in range(5):
        try:
            d = client.get_ticker_details(ticker)
            out = {"symbol": ticker}
            for src, dest in (
                ("name", "name"),
                ("sic_code", "sic"),
                ("sic", "sic"),
                ("sic_description", "sic_description"),
                ("type", "type"),
                ("primary_exchange", "primary_exchange"),
            ):
                if hasattr(d, src):
                    val = getattr(d, src)
                    if val is not None and dest not in out:
                        out[dest] = val
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out), encoding="utf-8")
            return out
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(2.0 * (attempt + 1))
    return {"symbol": ticker, "error": last_err}


def row_from_bars(sym: str, session: dict, prev: dict | None) -> dict | None:
    high = _f(session.get("high"))
    low = _f(session.get("low"))
    last = _f(session.get("close"))
    volume = _f(session.get("volume")) or 0.0
    vwap = _f(session.get("vwap"))
    day_open = _f(session.get("open"))
    prev_close = _f(prev.get("close")) if prev else None
    if last is None or last <= 0 or high is None or low is None:
        return None
    tr_parts = [high - low]
    if prev_close and prev_close > 0:
        tr_parts.append(abs(high - prev_close))
        tr_parts.append(abs(low - prev_close))
    tr = max(tr_parts)
    atr_pct = tr / last
    dollar_volume = (vwap * volume) if vwap and vwap > 0 and volume > 0 else last * volume
    gap_pct = None
    if prev_close and prev_close > 0 and day_open and day_open > 0:
        gap_pct = (day_open - prev_close) / prev_close
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
        "atr_pct": atr_pct,
        "dollar_volume": dollar_volume,
        "gap_pct": gap_pct,
        "change_pct": change_pct,
        "price_source": f"massive_grouped_daily:{SESSION_DATE}",
        "session_date": SESSION_DATE,
        "prev_date": PREV_DATE,
    }


def main() -> int:
    symbols = load_symbols(UNIVERSE_PATH)
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    key = cfg.get("massive_api_key")
    if not key:
        print("ERROR: massive_api_key missing from trader.yaml", file=sys.stderr)
        return 1
    client = RESTClient(api_key=key)

    print(f"universe={len(symbols)}", flush=True)
    session_map = fetch_grouped(client, SESSION_DATE)
    time.sleep(2)
    prev_map = fetch_grouped(client, PREV_DATE)

    by_sym: dict[str, dict] = {}
    missing: list[str] = []
    for sym in symbols:
        sess = session_map.get(sym)
        if sess is None:
            missing.append(sym)
            continue
        row = row_from_bars(sym, sess, prev_map.get(sym))
        if row is None:
            missing.append(sym)
            continue
        by_sym[sym] = row

    print(f"valid={len(by_sym)} missing={len(missing)}", flush=True)
    print("missing:", " ".join(missing), flush=True)

    print("fetching ticker details...", flush=True)
    details_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(fetch_details, client, s): s for s in by_sym}
        done = 0
        for fut in as_completed(futs):
            d = fut.result()
            details_map[d.get("symbol") or futs[fut]] = d
            done += 1
            if done % 40 == 0:
                print(f"  details {done}/{len(by_sym)}", flush=True)

    ranked_pool: list[dict] = []
    biotech_dropped: list[dict] = []
    for sym, row in by_sym.items():
        d = details_map.get(sym, {})
        sic = str(d.get("sic") or "")
        sic_desc = str(d.get("sic_description") or "")
        name = str(d.get("name") or "")
        rec = {
            **row,
            "name": name,
            "sic": sic,
            "sic_description": sic_desc,
            "industry": sic_desc,
            "excluded_reason": None,
        }
        bio, reason = is_biotech(d)
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

    as_of = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    payload = {
        "as_of": as_of,
        "screen": {
            "definition": (
                "top 25% of 0.5*pct_rank(session TR%) + 0.5*pct_rank(dollar volume); "
                "biotech (SIC 2836 / biological products) excluded before ranking"
            ),
            "price_source": (
                f"Massive grouped daily {SESSION_DATE} vs prev close {PREV_DATE}. "
                "get_snapshot_all not entitled on this Massive plan; today's developing "
                "session is also gated (EOD-only). Ranked on last completed RTH session "
                "(2026-09-04; 2026-09-07 Labor Day, 2026-09-08 in-progress)."
            ),
            "session_date": SESSION_DATE,
            "prev_date": PREV_DATE,
            "universe_n": len(symbols),
            "valid_snapshots": len(by_sym),
            "missing_snapshot": len(missing),
            "biotech_dropped": len(biotech_dropped),
            "ranked_pool": len(ranked_pool),
            "survivors": len(survivors),
        },
        "survivors": survivors,
        "biotech_dropped": [
            {
                "symbol": r["symbol"],
                "name": r.get("name"),
                "sic": r.get("sic"),
                "sic_description": r.get("sic_description"),
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
    print("survivors:", " ".join(r["symbol"] for r in survivors))
    print("biotech:", " ".join(r["symbol"] for r in sorted(biotech_dropped, key=lambda x: x["symbol"])))
    print("wrote", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
