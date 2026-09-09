"""LEARN MIX for EOD-flat *intraday* strategies on 1-min bars.

Daily bars produced 0 trades for gap/VWAP/ORB. This pass downloads 60d of
1-min history and scores only books that flatten by 15:45/15:55 ET.

Never places orders. Default params only.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from learn_mix_factory import (  # noqa: E402
    SYMBOLS,
    _jsonable_float,
    _read_bars,
    _fetch_history,
    confidence_for_run,
    decide,
)

CHECKPOINT = ROOT / "scripts" / "learn_mix_intraday_checkpoint.json"
DAILY_CKPT = ROOT / "scripts" / "learn_mix_checkpoint.json"
DAYS = 60
BAR = "1 min"
MIN_BARS_RUN = 4_000
FRESH_ROWS = MIN_BARS_RUN  # Databento BASIC 1-min is thinner than Massive SIP

WORKERS = 6
NOTE = "factory-learn-mix-intraday {symbol} 1min 60d EOD-flat defaults"

# Only strategies that flatten EOD (close_by_time or eod_flat SELL).
STRATS = [
    ("strategies/gap_reversion.py", "GapReversion"),
    ("strategies/vwap_reversion.py", "VwapReversion"),
    ("strategies/vwap_reclaim.py", "VwapReclaim"),
    ("strategies/opening_range_breakout.py", "OpeningRangeBreakout"),
    ("strategies/opening_drive_fade.py", "OpeningDriveFade"),
    ("strategies/rsi_atr_range.py", "RsiAtrRange"),
    ("strategies/late_day_momentum.py", "LateDayMomentum"),
    ("strategies/mean_reversion_intraday.py", "MeanReversionIntraday"),
    ("strategies/momentum_intraday.py", "MomentumIntraday"),
    ("strategies/keltner_breakout_intraday.py", "KeltnerBreakoutIntraday"),
]


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def load_ckpt() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    return {
        "asOf": None,
        "phase": "start",
        "bar_size": BAR,
        "days": DAYS,
        "strategies": [c for _, c in STRATS],
        "symbols": {},
        "errors": [],
    }


def save_ckpt(ck: dict) -> None:
    ck["asOf"] = _now()
    CHECKPOINT.write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
    print(f"[ckpt] {CHECKPOINT} phase={ck.get('phase')}", flush=True)


def load_conids() -> dict[str, int | None]:
    if not DAILY_CKPT.exists():
        return {s: None for s in SYMBOLS}
    daily = json.loads(DAILY_CKPT.read_text(encoding="utf-8"))
    out: dict[str, int | None] = {}
    for s in SYMBOLS:
        cid = (daily.get("symbols") or {}).get(s, {}).get("conId")
        out[s] = int(cid) if cid else None
    return out


def inspect_strategies() -> dict:
    import ast

    wanted = {cls for _, cls in STRATS}
    rows = []
    for rel, cls_name in STRATS:
        py = ROOT / rel
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src)
        mode = "on_prices"
        tunables: dict = {}
        docstring = ""
        eod = "close_by_time" in src or "EOD_" in src or "eod_flat" in src
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                docstring = ast.get_docstring(node) or ""
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "precompute":
                        mode = "precompute"
                    if isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name) and t.id.isupper():
                                try:
                                    tunables[t.id] = ast.literal_eval(item.value)
                                except Exception:
                                    pass
        rows.append({
            "file": rel,
            "class": cls_name,
            "mode": mode,
            "tunables": tunables,
            "eod_flat": eod,
            "docstring": docstring[:160],
        })
    missing = wanted - {r["class"] for r in rows}
    return {"strategies": rows, "missing": sorted(missing), "count": len(rows)}


def download_intraday(symbols: list[str], conids: dict[str, int | None], prior: dict | None = None) -> dict[str, dict]:
    from trader.container import Container
    from trader.data.data_access import TickStorage
    from trader.data.store import DateRange
    from trader.listeners.databento_history import (
        DatabentoHistoryWorker,
        load_databento_api_key,
    )
    from trader.listeners.massive_history import MassiveHistoryWorker
    from trader.objects import BarSize

    container = Container.instance()
    cfg = container.config()
    api_key = cfg.get("massive_api_key", "")
    if not api_key:
        raise RuntimeError("massive_api_key not configured")
    td_key = cfg.get("twelvedata_api_key", "")
    history_path = cfg.get("history_duckdb_path", "") or cfg.get("duckdb_path", "")
    storage = TickStorage(history_path)
    bar_size = BarSize.parse_str(BAR)
    tickdata = storage.get_tickdata(bar_size)
    massive = MassiveHistoryWorker(massive_api_key=api_key)
    td_worker = None
    if td_key:
        from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
        td_worker = TwelveDataHistoryWorker(twelvedata_api_key=td_key)
    db_worker = None
    db_key = load_databento_api_key(cfg.get("databento_api_key") or None)
    if db_key:
        try:
            db_worker = DatabentoHistoryWorker(api_key=db_key)
        except Exception as ex:
            print(f"databento worker unavailable: {ex}", flush=True)

    end_date = dt.datetime.now()
    start_date = end_date - dt.timedelta(days=DAYS)
    out: dict[str, dict] = dict(prior or {})
    massive_ok = True

    def _try_source(sym: str, prefer: str):
        if prefer == "databento":
            if db_worker is None:
                raise RuntimeError("databento not configured")
            df = db_worker.get_history(sym, bar_size, start_date, end_date)
            return df, "databento"
        return _fetch_history(sym, bar_size, start_date, end_date, massive, td_worker, prefer)

    for i, sym in enumerate(symbols, 1):
        prev = out.get(sym) or {}
        if prev.get("status") in ("ok", "fresh") and (prev.get("rows") or 0) >= FRESH_ROWS:
            print(f"  [{i}/{len(symbols)}] {sym}: resume {prev['status']} {prev.get('rows')}", flush=True)
            continue

        keys = []
        cid = conids.get(sym)
        if cid:
            keys.append(cid)
        keys.append(sym)
        n_exist, last, used_key = 0, None, keys[0]
        for k in keys:
            n, lst = _read_bars(tickdata, k, start_date, end_date)
            if n > n_exist:
                n_exist, last, used_key = n, lst, k
        if n_exist >= FRESH_ROWS:
            write_key = cid if cid else used_key
            if cid and str(used_key) != str(cid):
                df_exist = tickdata.read(used_key, date_range=DateRange(start=start_date, end=end_date))
                if df_exist is not None and not df_exist.empty:
                    tickdata.write_resolve_overlap(cid, df_exist)
                    write_key = cid
            out[sym] = {
                "status": "fresh",
                "rows": n_exist,
                "last": last,
                "key": str(write_key),
                "wrote": 0,
                "source": "local",
            }
            print(f"  [{i}/{len(symbols)}] {sym}: fresh {n_exist} last={last}", flush=True)
            continue

        write_key = cid if cid else sym
        order = []
        if db_worker and (not massive_ok or n_exist == 0):
            order.append("databento")
        if massive_ok:
            order.append("massive")
        if db_worker and "databento" not in order:
            order.append("databento")
        if td_worker:
            order.append("twelvedata")
        if not order:
            order.append("massive")

        last_ex: Exception | None = None
        wrote = 0
        src = order[0]
        n_after, last2 = n_exist, last
        for prefer in order:
            try:
                df, src = _try_source(sym, prefer)
                if df is not None and not df.empty:
                    if "symbol" in df.columns:
                        df = df.drop(columns=["symbol"])
                    tickdata.write_resolve_overlap(write_key, df)
                    wrote = len(df)
                n_after, last2 = _read_bars(tickdata, write_key, start_date, end_date)
                if n_after or wrote:
                    last_ex = None
                    break
            except Exception as ex:
                last_ex = ex
                if "429" in str(ex):
                    massive_ok = False
                print(f"  [{i}/{len(symbols)}] {sym}: {prefer} failed {ex}", flush=True)
                continue
        if last_ex and not n_after:
            n_exist, last = _read_bars(tickdata, write_key, start_date, end_date)
            out[sym] = {
                "status": "error",
                "rows": n_exist,
                "last": last,
                "key": str(write_key),
                "wrote": 0,
                "error": f"{type(last_ex).__name__}: {last_ex}",
            }
            print(f"  [{i}/{len(symbols)}] {sym}: ERROR {last_ex}", flush=True)
            time.sleep(1.5)
            continue
        out[sym] = {
            "status": "ok" if n_after else "empty",
            "rows": n_after,
            "last": last2,
            "key": str(write_key),
            "wrote": wrote,
            "source": src,
        }
        print(f"  [{i}/{len(symbols)}] {sym}: {src} wrote {wrote} now {n_after} last={last2}", flush=True)
        time.sleep(0.2)
    return out


def run_one_backtest(rel: str, class_name: str, conid: int, symbol: str) -> dict:
    import json as _json
    from trader.container import Container
    from trader.data.backtest_store import BacktestRecord, BacktestStore, compute_strategy_hash
    from trader.data.data_access import TickStorage
    from trader.data.universe import UniverseAccessor
    from trader.objects import BarSize
    from trader.simulation.backtester import BacktestConfig, Backtester
    from trader.simulation.slippage import get_slippage_model

    container = Container.instance()
    cfg = container.config()
    duckdb_path = cfg.get("duckdb_path", "")
    history_path = cfg.get("history_duckdb_path", "") or duckdb_path
    storage = TickStorage(history_path)
    accessor = UniverseAccessor(duckdb_path, cfg.get("universe_library", "Universes"))
    bar_size = BarSize.parse_str(BAR)
    path = str(ROOT / rel)

    config = BacktestConfig(
        start_date=dt.datetime.now() - dt.timedelta(days=DAYS),
        end_date=dt.datetime.now(),
        initial_capital=100000,
        bar_size=bar_size,
        slippage_model=get_slippage_model("fixed", bps=1.0),
    )
    backtester = Backtester(storage, config)
    result = backtester.run_from_module(path, class_name, [conid], universe_accessor=accessor)

    trades_json = _json.dumps([{
        "timestamp": str(t.timestamp),
        "conid": int(t.conid),
        "action": str(t.action),
        "quantity": float(t.quantity),
        "price": float(t.price),
        "commission": float(t.commission),
        "signal_probability": float(t.signal_probability),
        "signal_risk": float(t.signal_risk),
    } for t in result.trades])
    equity_curve_json = _json.dumps([
        {"timestamp": str(ts), "value": float(v)}
        for ts, v in result.equity_curve.items()
    ])
    final_equity = (
        float(result.equity_curve.iloc[-1])
        if len(result.equity_curve) > 0 else 100000.0
    )
    record = BacktestRecord(
        strategy_path=path,
        class_name=class_name,
        conids=[conid],
        universe="",
        start_date=config.start_date,
        end_date=config.end_date,
        sortino_ratio=result.sortino_ratio,
        calmar_ratio=result.calmar_ratio,
        profit_factor=result.profit_factor,
        expectancy_bps=result.expectancy_bps,
        time_in_market_pct=result.time_in_market_pct,
        bar_size=str(bar_size),
        initial_capital=config.initial_capital,
        fill_policy=config.fill_policy,
        slippage_bps=1.0,
        commission_per_share=config.commission_per_share,
        params=dict(getattr(result, "applied_params", {}) or {}),
        code_hash=compute_strategy_hash(path),
        total_trades=result.total_trades,
        total_return=result.total_return,
        sharpe_ratio=result.sharpe_ratio,
        max_drawdown=result.max_drawdown,
        win_rate=result.win_rate,
        final_equity=final_equity,
        trades_json=trades_json,
        equity_curve_json=equity_curve_json,
        note=NOTE.format(symbol=symbol),
        sweep_id=None,
    )
    run_id = BacktestStore(duckdb_path).add(record)
    return {
        "status": "ok",
        "class": class_name,
        "run_id": run_id,
        "total_trades": result.total_trades,
        "total_return": result.total_return,
        "sharpe_ratio": result.sharpe_ratio,
        "sortino_ratio": result.sortino_ratio,
        "profit_factor": _jsonable_float(result.profit_factor),
        "expectancy_bps": result.expectancy_bps,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate if result.total_trades > 0 else None,
        "params": dict(getattr(result, "applied_params", {}) or {}),
    }


def _job(payload: tuple) -> tuple[str, dict]:
    rel, cls, conid, symbol = payload
    try:
        return symbol, run_one_backtest(rel, cls, int(conid), symbol)
    except Exception as ex:
        return symbol, {
            "status": "error",
            "class": cls,
            "error": f"{type(ex).__name__}: {ex}",
        }


def recommend(runs: list[dict]) -> dict:
    scored = []
    for r in runs:
        if r.get("status") != "ok":
            continue
        conf = r.get("confidence") or {}
        dec = r.get("decision") or {}
        trades = r.get("total_trades") or 0
        if trades < 3:
            continue
        psr = conf.get("probabilistic_sharpe")
        ret = r.get("total_return") or 0.0
        score = (1.0 if dec.get("cleared") else 0.4 if dec.get("watch") else 0.1)
        score *= (psr if psr is not None else 0.0)
        score *= (1.0 + max(ret, -0.5))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [r for _, r in scored[:3] if r.get("decision", {}).get("cleared") or r.get("decision", {}).get("watch")]
    cleared = [r for r in picked if r.get("decision", {}).get("cleared")]
    return {
        "picks": [{
            "class": r["class"],
            "run_id": r.get("run_id"),
            "params": r.get("params") or {},
            "total_trades": r.get("total_trades"),
            "total_return": r.get("total_return"),
            "sharpe_ratio": r.get("sharpe_ratio"),
            "probabilistic_sharpe": (r.get("confidence") or {}).get("probabilistic_sharpe"),
            "return_ci_lo": (r.get("confidence") or {}).get("return_ci_lo"),
            "return_ci_hi": (r.get("confidence") or {}).get("return_ci_hi"),
            "p_value": (r.get("confidence") or {}).get("p_value"),
            "cleared": bool((r.get("decision") or {}).get("cleared")),
            "watch": bool((r.get("decision") or {}).get("watch")),
            "deployable": bool((r.get("decision") or {}).get("deployable")),
            "flags": (r.get("decision") or {}).get("flags") or [],
        } for r in picked],
        "any_cleared": bool(cleared),
        "any_watch": any((r.get("decision") or {}).get("watch") for r in picked),
        "note": (
            "EOD-flat 1-min books only. Top 1–3 with PSR≥0.95 + CI>0 + trades≥10. "
            "Watch-only is evidence, not deploy."
            if picked else
            "No EOD-flat 1-min strategy produced enough trades/confidence on 60d."
        ),
    }


def main() -> int:
    ck = load_ckpt()
    ck["bar_size"] = BAR
    ck["days"] = DAYS
    print("=== inspect EOD-flat intraday ===", flush=True)
    ck["inspect"] = inspect_strategies()
    ck["phase"] = "inspect"
    save_ckpt(ck)
    print(json.dumps({
        "count": ck["inspect"]["count"],
        "eod": {r["class"]: r["eod_flat"] for r in ck["inspect"]["strategies"]},
    }, indent=2), flush=True)

    print("=== conIds from daily checkpoint ===", flush=True)
    conids = load_conids()
    n_ok = sum(1 for s in SYMBOLS if conids.get(s))
    ck["phase"] = "resolve"
    for sym in SYMBOLS:
        rec = ck["symbols"].setdefault(sym, {})
        rec["conId"] = conids.get(sym)
        if rec["conId"] is None:
            rec["preflight"] = "blocked"
            rec["block_reason"] = "conId missing from daily checkpoint"
        else:
            rec["preflight"] = "ok"
            rec.pop("block_reason", None)
    save_ckpt(ck)
    print(f"resolved {n_ok}/{len(SYMBOLS)}", flush=True)

    print("=== data_download 60d 1-min ===", flush=True)
    ck["phase"] = "download"
    save_ckpt(ck)
    prior_dl = {
        s: (ck["symbols"].get(s) or {}).get("data")
        for s in SYMBOLS
        if isinstance((ck["symbols"].get(s) or {}).get("data"), dict)
    }
    prior_dl = {k: v for k, v in prior_dl.items() if v}
    dl = download_intraday(SYMBOLS, conids, prior=prior_dl)
    for sym, info in dl.items():
        ck["symbols"].setdefault(sym, {}).update({"data": info})
    save_ckpt(ck)

    runnable = [
        s for s in SYMBOLS
        if conids.get(s) and (dl.get(s) or {}).get("rows", 0) >= MIN_BARS_RUN
    ]
    blocked_data = [
        s for s in SYMBOLS
        if conids.get(s) and (dl.get(s) or {}).get("rows", 0) < MIN_BARS_RUN
    ]
    for s in blocked_data:
        ck["symbols"][s]["preflight"] = "blocked"
        ck["symbols"][s]["block_reason"] = (
            f"1-min bars insufficient: {(dl.get(s) or {}).get('rows', 0)}"
        )
    save_ckpt(ck)
    print(f"runnable {len(runnable)} blocked_data {len(blocked_data)}", flush=True)

    print(f"=== backtest_batch 1-min EOD-flat workers={WORKERS} ===", flush=True)
    ck["phase"] = "backtest"
    save_ckpt(ck)
    jobs = []
    for sym in runnable:
        rec = ck["symbols"][sym]
        rec["learn_state"] = "active"
        rec["runs"] = rec.get("runs") or []
        already = {r.get("class") for r in rec["runs"] if r.get("status") == "ok"}
        for rel, cls in STRATS:
            if cls in already:
                continue
            jobs.append((rel, cls, int(conids[sym]), sym))
    print(f"jobs {len(jobs)}", flush=True)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(_job, j): j for j in jobs}
        for fut in as_completed(futs):
            rel, cls, _cid, _sym = futs[fut]
            try:
                symbol, row = fut.result()
            except Exception as ex:
                symbol, row = _sym, {
                    "status": "error",
                    "class": cls,
                    "error": f"{type(ex).__name__}: {ex}",
                }
            rec = ck["symbols"][symbol]
            rec["runs"] = [r for r in (rec.get("runs") or []) if r.get("class") != row.get("class")]
            rec["runs"].append(row)
            done += 1
            if row.get("status") != "ok":
                print(f"    {symbol} {row.get('class')} {row.get('status')} {row.get('error', '')}", flush=True)
            if done % 10 == 0 or done == len(jobs):
                save_ckpt(ck)
                print(f"  jobs {done}/{len(jobs)} {time.time() - t0:.0f}s", flush=True)
    for sym in runnable:
        ck["symbols"][sym]["learn_state"] = "batch_done"
    save_ckpt(ck)

    print("=== backtests_confidence ===", flush=True)
    ck["phase"] = "confidence"
    save_ckpt(ck)
    for si, sym in enumerate(runnable, 1):
        rec = ck["symbols"][sym]
        runs = rec.get("runs") or []
        ranked = sorted(
            [r for r in runs if r.get("status") == "ok"],
            key=lambda r: (r.get("total_trades") or 0, r.get("total_return") or -9),
            reverse=True,
        )
        top = ranked[:4]
        for r in runs:
            if r.get("status") != "ok":
                r["confidence"] = None
                r["decision"] = {"cleared": False, "watch": False, "deployable": False, "flags": ["run_failed"]}
                continue
            if r in top and r.get("run_id") and r.get("total_trades", 0) >= 3:
                try:
                    r["confidence"] = confidence_for_run(int(r["run_id"]))
                except Exception as ex:
                    r["confidence"] = {"error": f"{type(ex).__name__}: {ex}"}
            else:
                r["confidence"] = {
                    "skipped": True,
                    "reason": "not in top-4 or trades < 3 — not fabricating PSR",
                }
            r["decision"] = decide(r, r.get("confidence") or {})
        rec["recommended"] = recommend(runs)
        rec["learn_state"] = "done"
        rec["mix_complete"] = True
        if si % 5 == 0 or si == len(runnable):
            save_ckpt(ck)
            print(f"  conf {si}/{len(runnable)} {sym}", flush=True)

    for s in SYMBOLS:
        rec = ck["symbols"].setdefault(s, {})
        if rec.get("mix_complete"):
            continue
        if rec.get("preflight") == "blocked":
            rec["learn_state"] = "blocked"
            rec["mix_complete"] = False
            rec["recommended"] = {
                "picks": [],
                "any_cleared": False,
                "any_watch": False,
                "note": rec.get("block_reason") or "PRE-FLIGHT blocked",
            }
        else:
            rec["learn_state"] = rec.get("learn_state") or "queued"
            rec["mix_complete"] = False

    complete = sum(1 for s in SYMBOLS if ck["symbols"].get(s, {}).get("mix_complete"))
    ck["counts"] = {
        "agents": 74,
        "resolved": n_ok,
        "runnable": len(runnable),
        "mix_complete": complete,
        "bar_size": BAR,
        "days": DAYS,
    }
    ck["phase"] = "done"
    save_ckpt(ck)
    print(f"DONE intraday mix_complete={complete}/74", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
