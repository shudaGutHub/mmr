"""Fill missing 1-min bars for LEARN MIX via Databento (Alpaca IEX fallback).

Reads scripts/learn_mix_intraday_checkpoint.json for names with < MIN_BARS
rows, writes TickData keyed by conId, does not place orders.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pandas as pd
import pytz

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from learn_mix_factory import SYMBOLS, _read_bars  # noqa: E402
from learn_mix_intraday import (  # noqa: E402
    CHECKPOINT,
    DAYS,
    MIN_BARS_RUN,
    load_conids,
)
from screen_alpaca_factory import alpaca_headers, fetch_bars, merge_envs  # noqa: E402

BAR = "1 min"
BATCH = 1  # multi-symbol get_range truncates near ~50k rows


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def need_symbols(ck: dict) -> list[str]:
    out = []
    for sym in SYMBOLS:
        rec = (ck.get("symbols") or {}).get(sym) or {}
        if rec.get("mix_complete"):
            continue
        out.append(sym)
    return out


def alpaca_to_tick(rows: list[dict], bar_size: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    tz = pytz.timezone("US/Eastern")
    recs = []
    for b in rows:
        ts = b.get("t") or b.get("timestamp")
        if not ts:
            continue
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert(tz)
        o, h, l, c = b.get("o"), b.get("h"), b.get("l"), b.get("c")
        if None in (o, h, l, c):
            continue
        vw = b.get("vw")
        average = vw if vw is not None else (float(h) + float(l) + float(c)) / 3.0
        recs.append({
            "date": stamp,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(b.get("v") or 0),
            "average": float(average),
            "bar_count": int(b.get("n") or 0),
            "bar_size": bar_size,
            "what_to_show": 1,
        })
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).set_index("date").sort_index()
    return df[~df.index.duplicated(keep="first")]


def main() -> int:
    from trader.container import Container
    from trader.data.data_access import TickStorage
    from trader.listeners.databento_history import (
        DatabentoHistoryWorker,
        load_databento_api_key,
    )
    from trader.objects import BarSize

    ck = json.loads(CHECKPOINT.read_text(encoding="utf-8")) if CHECKPOINT.exists() else {}
    missing = need_symbols(ck)
    conids = load_conids()
    print(f"fill targets {len(missing)}/{len(SYMBOLS)}  days={DAYS}  bar={BAR}", flush=True)
    if not missing:
        print("nothing to fill", flush=True)
        return 0

    db_key = load_databento_api_key()
    if not db_key:
        raise RuntimeError("DATABENTO_API_KEY not found in env or secrets")
    worker = DatabentoHistoryWorker(api_key=db_key)

    env = merge_envs()
    headers = None
    try:
        headers = alpaca_headers(env)
    except Exception as ex:
        print(f"alpaca fallback unavailable: {ex}", flush=True)

    container = Container.instance()
    cfg = container.config()
    history_path = cfg.get("history_duckdb_path", "") or cfg.get("duckdb_path", "")
    tickdata = TickStorage(history_path).get_tickdata(BarSize.parse_str(BAR))

    end_date = dt.datetime.now()
    start_date = end_date - dt.timedelta(days=DAYS)
    start_iso = start_date.replace(tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_date.replace(tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    filled = 0
    still_short: list[str] = []
    for i in range(0, len(missing), BATCH):
        chunk = missing[i : i + BATCH]
        print(f"databento batch {i // BATCH + 1} {chunk}", flush=True)
        try:
            many = worker.get_history_many(
                chunk, BarSize.Mins1, start_date, end_date, timezone="US/Eastern",
            )
        except Exception as ex:
            print(f"  databento batch error: {type(ex).__name__}: {ex}", flush=True)
            many = {s: pd.DataFrame() for s in chunk}

        alpaca_need = []
        for s in chunk:
            part = many.get(s.upper()) if many.get(s.upper()) is not None else many.get(s)
            n_db = 0 if part is None or part.empty else len(part)
            cid = conids.get(s) or s
            n_exist, _ = _read_bars(tickdata, cid, start_date, end_date)
            if max(n_db, n_exist) < MIN_BARS_RUN:
                alpaca_need.append(s)
        alpaca_bars: dict[str, list] = {}
        if alpaca_need and headers:
            print(f"  alpaca IEX fallback {alpaca_need}", flush=True)
            try:
                alpaca_bars = fetch_bars(headers, alpaca_need, "iex", "1Min", start_iso, end_iso)
            except Exception as ex:
                print(f"  alpaca error: {type(ex).__name__}: {ex}", flush=True)

        for sym in chunk:
            cid = conids.get(sym)
            write_key = cid if cid else sym
            df = many.get(sym.upper()) if many.get(sym.upper()) is not None else many.get(sym)
            source = "databento"
            wrote = 0
            if df is not None and not df.empty:
                if "symbol" in df.columns:
                    df = df.drop(columns=["symbol"])
                tickdata.write_resolve_overlap(write_key, df)
                wrote = len(df)
                source = "databento"
            n_mid, _ = _read_bars(tickdata, write_key, start_date, end_date)
            alpaca_df = alpaca_to_tick(
                alpaca_bars.get(sym) or alpaca_bars.get(sym.upper()) or [], BAR,
            )
            if n_mid < MIN_BARS_RUN and alpaca_df is not None and not alpaca_df.empty:
                tickdata.write_resolve_overlap(write_key, alpaca_df)
                wrote += len(alpaca_df)
                source = "databento+alpaca" if wrote and n_mid else "alpaca"
            n_after, last = _read_bars(tickdata, write_key, start_date, end_date)
            rec = ck.setdefault("symbols", {}).setdefault(sym, {})
            rec["data"] = {
                "status": "ok" if n_after >= MIN_BARS_RUN else ("empty" if not n_after else "thin"),
                "rows": n_after,
                "last": last,
                "key": str(write_key),
                "wrote": wrote,
                "source": source,
            }
            if n_after >= MIN_BARS_RUN:
                rec["preflight"] = "ok"
                rec.pop("block_reason", None)
                rec["learn_state"] = "queued"
                rec["mix_complete"] = False
                filled += 1
            else:
                rec["preflight"] = "blocked"
                rec["block_reason"] = f"1-min bars insufficient: {n_after}"
                still_short.append(sym)
            print(
                f"  {sym} {source} wrote={wrote} now={n_after} last={last} key={write_key}",
                flush=True,
            )
        ck["asOf"] = _now()
        CHECKPOINT.write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
        time.sleep(0.3)

    print(f"DONE filled>={MIN_BARS_RUN}: {filled}/{len(missing)} still_short={still_short}", flush=True)
    return 0 if not still_short else 2


if __name__ == "__main__":
    raise SystemExit(main())
