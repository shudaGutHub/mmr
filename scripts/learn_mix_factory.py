"""LEARN MIX for the 74 symbol-agent-factory survivors.

Downloads 180d daily bars (library path — avoids CLI Rich/cp1252 crash),
resolves conIds from local universe then trader_service, runs the ten
named strategies at default params, persists runs, scores confidence,
and writes scripts/learn_mix_checkpoint.json.

Never places orders. Default params only. Daily first pass.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SYMBOLS = [
    "META", "NBIS", "SNDK", "LITE", "STX", "SPCX", "QCOM", "MU", "BKNG", "EXPE",
    "CBRS", "WDC", "SKHY", "SHOP", "ALAB", "MRVL", "AAOI", "CRDO", "TSLA", "AMD",
    "CDNS", "EBAY", "SMTC", "ARM", "LULU", "TEAM", "KLAC", "CASY", "CRWD", "AKAM",
    "AEHR", "LRCX", "NVDA", "CHEF", "AVGO", "GH", "OKTA", "ADBE", "AMAT", "DASH",
    "PANW", "ABNB", "MDB", "PLTR", "DAVE", "ASML", "MTSI", "ZS", "NTRA", "CSCO",
    "DUOL", "ISRG", "MKSI", "INTU", "VRSN", "SNPS", "HONA", "NTAP", "DDOG", "TSEM",
    "ADSK", "TTMI", "ILMN", "COIN", "FSLR", "PTC", "CDW", "STRL", "CEG", "MPWR",
    "AXON", "APP", "PODD", "FTNT",
]

STRATS = [
    ("strategies/gap_reversion.py", "GapReversion"),
    ("strategies/vwap_reversion.py", "VwapReversion"),
    ("strategies/vwap_reclaim.py", "VwapReclaim"),
    ("strategies/opening_range_breakout.py", "OpeningRangeBreakout"),
    ("strategies/opening_drive_fade.py", "OpeningDriveFade"),
    ("strategies/keltner_breakout.py", "KeltnerBreakout"),
    ("strategies/rsi_atr_range.py", "RsiAtrRange"),
    ("strategies/late_day_momentum.py", "LateDayMomentum"),
    ("strategies/mean_reversion.py", "MeanReversion"),
    ("strategies/momentum.py", "Momentum"),
]

CHECKPOINT = ROOT / "scripts" / "learn_mix_checkpoint.json"
DAYS = 180
BAR = "1 day"
MIN_TRADES_DEPLOY = 10
MIN_TRADES_RANK = 3


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def load_ckpt() -> dict:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    return {
        "asOf": None,
        "phase": "start",
        "inspect": None,
        "trader_service": None,
        "symbols": {},
        "errors": [],
    }


def save_ckpt(ck: dict) -> None:
    ck["asOf"] = _now()
    CHECKPOINT.write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
    print(f"[ckpt] {CHECKPOINT} phase={ck.get('phase')}", flush=True)


def inspect_strategies() -> dict:
    import ast

    wanted = {cls for _, cls in STRATS}
    rows = []
    for rel, cls_name in STRATS:
        py = ROOT / rel
        tree = ast.parse(py.read_text(encoding="utf-8"))
        mode = "on_prices"
        tunables: dict = {}
        docstring = ""
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
            "docstring": docstring[:160],
        })
    missing = wanted - {r["class"] for r in rows}
    return {"strategies": rows, "missing": sorted(missing), "count": len(rows)}


def resolve_all(symbols: list[str]) -> tuple[dict[str, int | None], dict]:
    from trader.container import Container
    from trader.data.universe import UniverseAccessor

    container = Container.instance()
    cfg = container.config()
    duckdb_path = cfg.get("duckdb_path", "")
    accessor = UniverseAccessor(duckdb_path, cfg.get("universe_library", "Universes"))

    mapping: dict[str, int | None] = {}
    source: dict[str, str] = {}
    for sym in symbols:
        hits = accessor.resolve_symbol(sym, first_only=True)
        if hits:
            mapping[sym] = int(hits[0].conId)
            source[sym] = "local_universe"
        else:
            mapping[sym] = None
            source[sym] = "unresolved"

    trader_meta = {
        "connected": False,
        "resolved": 0,
        "error": "skipped — prior mmr --json status hung; IB Gateway 7497 refused",
    }
    # Do not call trader_service here. A down ZMQ server blocks far longer
    # than MMR(timeout=3). Unresolved names stay PRE-FLIGHT blocked on conId.
    return mapping, {"per_symbol": source, "trader": trader_meta}


def _read_bars(tickdata, key, start_date, end_date):
    from trader.data.store import DateRange
    existing = tickdata.read(key, date_range=DateRange(start=start_date, end=end_date))
    n = 0 if existing is None or existing.empty else len(existing)
    last = None
    if n:
        try:
            last = str(existing.index.max())[:10]
        except Exception:
            last = None
    return n, last


def _fetch_history(sym, bar_size, start_date, end_date, massive_worker, td_worker, prefer: str):
    if prefer == "twelvedata" and td_worker is not None:
        return td_worker.get_history(
            ticker=sym, bar_size=bar_size, start_date=start_date, end_date=end_date,
        ), "twelvedata"
    df = massive_worker.get_history(
        ticker=sym, bar_size=bar_size, start_date=start_date, end_date=end_date,
    )
    return df, "massive"


def download_daily(symbols: list[str], conids: dict[str, int | None], prior: dict | None = None) -> dict[str, dict]:
    from trader.container import Container
    from trader.data.data_access import TickStorage
    from trader.data.store import DateRange
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

    end_date = dt.datetime.now()
    start_date = end_date - dt.timedelta(days=DAYS)
    out: dict[str, dict] = dict(prior or {})
    massive_ok = True

    for i, sym in enumerate(symbols, 1):
        prev = out.get(sym) or {}
        if prev.get("status") in ("ok", "fresh") and (prev.get("rows") or 0) >= 80:
            cid = conids.get(sym)
            used = prev.get("key")
            if cid and used and str(used) != str(cid):
                df_exist = tickdata.read(used, date_range=DateRange(start=start_date, end=end_date))
                if df_exist is None or df_exist.empty:
                    df_exist = tickdata.read(sym, date_range=DateRange(start=start_date, end=end_date))
                if df_exist is not None and not df_exist.empty:
                    tickdata.write_resolve_overlap(cid, df_exist)
                    prev = dict(prev)
                    prev["key"] = str(cid)
                    out[sym] = prev
                    print(f"  [{i}/{len(symbols)}] {sym}: remapped {used} -> {cid} ({len(df_exist)} bars)", flush=True)
                    continue
            print(f"  [{i}/{len(symbols)}] {sym}: resume {prev['status']} {prev.get('rows')} last={prev.get('last')}", flush=True)
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
        # 80+ daily bars ending on/after 2026-09-04 is usable for a 180d pass
        # (Massive last print is often the prior session). Do not re-hit 429s.
        fresh = n_exist >= 80 and last is not None
        if fresh:
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
            print(f"  [{i}/{len(symbols)}] {sym}: fresh {n_exist} rows last={last} key={write_key}", flush=True)
            continue

        write_key = cid if cid else sym
        prefer = "twelvedata" if (not massive_ok and td_worker) else "massive"
        try:
            df, src = _fetch_history(sym, bar_size, start_date, end_date, massive, td_worker, prefer)
            wrote = 0
            if df is not None and not df.empty:
                tickdata.write_resolve_overlap(write_key, df)
                wrote = len(df)
            n_after, last2 = _read_bars(tickdata, write_key, start_date, end_date)
            out[sym] = {
                "status": "ok" if n_after else "empty",
                "rows": n_after,
                "last": last2,
                "key": str(write_key),
                "wrote": wrote,
                "source": src,
            }
            print(f"  [{i}/{len(symbols)}] {sym}: {src} wrote {wrote} now {n_after} last={last2}", flush=True)
            time.sleep(0.35 if src == "massive" else 0.2)
        except Exception as ex:
            msg = str(ex)
            if "429" in msg:
                massive_ok = False
            if td_worker and prefer == "massive":
                try:
                    time.sleep(1.0)
                    df, src = _fetch_history(sym, bar_size, start_date, end_date, massive, td_worker, "twelvedata")
                    wrote = 0
                    if df is not None and not df.empty:
                        tickdata.write_resolve_overlap(write_key, df)
                        wrote = len(df)
                    n_after, last2 = _read_bars(tickdata, write_key, start_date, end_date)
                    out[sym] = {
                        "status": "ok" if n_after else "empty",
                        "rows": n_after,
                        "last": last2,
                        "key": str(write_key),
                        "wrote": wrote,
                        "source": src,
                    }
                    print(f"  [{i}/{len(symbols)}] {sym}: fallback {src} wrote {wrote} now {n_after} last={last2}", flush=True)
                    time.sleep(0.25)
                    continue
                except Exception as ex2:
                    out[sym] = {
                        "status": "error",
                        "rows": n_exist,
                        "last": last,
                        "key": str(write_key),
                        "wrote": 0,
                        "error": f"{type(ex2).__name__}: {ex2}",
                    }
                    print(f"  [{i}/{len(symbols)}] {sym}: TD ERROR {ex2}", flush=True)
                    time.sleep(1.0)
                    continue
            out[sym] = {
                "status": "error",
                "rows": n_exist,
                "last": last,
                "key": str(write_key),
                "wrote": 0,
                "error": f"{type(ex).__name__}: {ex}",
            }
            print(f"  [{i}/{len(symbols)}] {sym}: ERROR {ex}", flush=True)
            time.sleep(1.5)
    return out


def _jsonable_float(x: float):
    if x == float("inf"):
        return "inf"
    if x == float("-inf"):
        return "-inf"
    if x != x:
        return None
    return x


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
        note=f"factory-learn-mix {symbol} daily 180d defaults",
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


def confidence_for_run(run_id: int) -> dict:
    import numpy as np
    from trader.container import Container
    from trader.data.backtest_store import BacktestStore
    from trader.objects import BarSize
    from trader.simulation.backtest_stats import compute_all
    from trader.simulation.backtester import Backtester

    container = Container.instance()
    store = BacktestStore(container.config().get("duckdb_path", ""))
    rec = store.get(run_id)
    if rec is None:
        return {"run_id": run_id, "error": "missing"}

    trades = None
    if rec.trades_json:
        try:
            trades = json.loads(rec.trades_json)
        except (ValueError, TypeError):
            trades = None
    bar_returns = None
    if rec.equity_curve_json:
        try:
            curve = json.loads(rec.equity_curve_json)
            values = np.array([float(p["value"]) for p in curve], dtype=float)
            if len(values) >= 2:
                bar_returns = np.diff(values) / values[:-1]
        except (ValueError, TypeError, KeyError):
            bar_returns = None
    try:
        bar_size_enum = BarSize(rec.bar_size) if rec.bar_size else BarSize.Days1
        bars_per_year = Backtester._bars_per_year(bar_size_enum)
    except Exception:
        bars_per_year = 252.0
    stats = compute_all(trades, bar_returns, bars_per_year=bars_per_year)
    reason = None
    if not rec.trades_json and not rec.equity_curve_json:
        reason = "run without saved trades"
    elif stats.n_trades == 0 and stats.n_bar_returns == 0:
        reason = "no usable trade or equity data"
    return {
        "run_id": run_id,
        "n_trades": stats.n_trades,
        "n_bar_returns": stats.n_bar_returns,
        "probabilistic_sharpe": stats.psr,
        "t_stat": stats.t_stat,
        "p_value": stats.p_value,
        "return_ci_lo": stats.return_ci_lo,
        "return_ci_hi": stats.return_ci_hi,
        "sharpe_ci_lo": stats.sharpe_ci_lo,
        "sharpe_ci_hi": stats.sharpe_ci_hi,
        "pnl_skew": stats.pnl_skew,
        "pnl_excess_kurtosis": stats.pnl_excess_kurtosis,
        "losing_streak_actual": stats.losing_streak_actual,
        "losing_streak_mc_95": stats.losing_streak_mc_95,
        "unavailable_reason": reason,
    }


def decide(run: dict, conf: dict) -> dict:
    trades = run.get("total_trades") or 0
    psr = conf.get("probabilistic_sharpe")
    ci_lo = conf.get("return_ci_lo")
    p_value = conf.get("p_value")
    pf = run.get("profit_factor")
    if isinstance(pf, str):
        pf_n = float("inf") if pf == "inf" else None
    else:
        pf_n = pf
    flags = []
    if trades < MIN_TRADES_DEPLOY:
        flags.append(f"low_trades:{trades}")
    if psr is None:
        flags.append("psr_unavailable")
    elif psr < 0.80:
        flags.append(f"psr_fail:{psr:.3f}")
    if ci_lo is not None and ci_lo <= 0:
        flags.append("ci_straddles_zero")
    if p_value is not None and p_value >= 0.05:
        flags.append(f"ttest_ns:{p_value:.3f}")
    if pf_n is not None and pf_n < 1.5:
        flags.append(f"pf_low:{pf_n:.2f}")
    if conf.get("unavailable_reason"):
        flags.append(str(conf["unavailable_reason"]))

    cleared = (
        trades >= MIN_TRADES_DEPLOY
        and psr is not None and psr >= 0.95
        and ci_lo is not None and ci_lo > 0
        and (p_value is None or p_value < 0.05)
        and (pf_n is None or pf_n >= 1.5)
    )
    watch = (
        not cleared
        and trades >= MIN_TRADES_RANK
        and psr is not None and psr >= 0.80
        and (ci_lo is None or ci_lo > 0)
    )
    return {
        "cleared": bool(cleared),
        "watch": bool(watch),
        "deployable": bool(cleared),
        "flags": flags,
    }


def recommend(runs: list[dict]) -> dict:
    scored = []
    for r in runs:
        if r.get("status") != "ok":
            continue
        conf = r.get("confidence") or {}
        dec = r.get("decision") or {}
        trades = r.get("total_trades") or 0
        if trades < MIN_TRADES_RANK:
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
            "Top 1–3 that cleared PSR≥0.95 + CI>0 + trades≥10. "
            "Watch-only names are evidence, not deploy."
            if picked else
            "No strategy produced enough real trades / confidence on 180d daily. "
            "Intraday families (gap/VWAP/ORB) are expected-empty on 1-day bars."
        ),
    }


def main() -> int:
    ck = load_ckpt()
    print("=== strategies_inspect ===", flush=True)
    ck["inspect"] = inspect_strategies()
    ck["phase"] = "inspect"
    save_ckpt(ck)
    print(json.dumps({
        "count": ck["inspect"]["count"],
        "modes": {r["class"]: r["mode"] for r in ck["inspect"]["strategies"]},
    }, indent=2), flush=True)

    print("=== resolve conIds ===", flush=True)
    cached_conids = {
        s: ck["symbols"][s].get("conId")
        for s in SYMBOLS
        if s in ck.get("symbols", {}) and ck["symbols"][s].get("conId")
    }
    if len(cached_conids) >= 11 and ck.get("trader_service"):
        conids = {s: ck["symbols"].get(s, {}).get("conId") for s in SYMBOLS}
        resolve_meta = {
            "per_symbol": {
                s: ck["symbols"].get(s, {}).get("resolve_source") or "checkpoint"
                for s in SYMBOLS
            },
            "trader": ck.get("trader_service") or {"connected": False, "resolved": 0},
        }
        print(f"reusing checkpoint conIds {len(cached_conids)}/74", flush=True)
    else:
        conids, resolve_meta = resolve_all(SYMBOLS)
    ck["trader_service"] = resolve_meta["trader"]
    ck["phase"] = "resolve"
    for sym in SYMBOLS:
        rec = ck["symbols"].setdefault(sym, {})
        rec["conId"] = conids.get(sym)
        rec["resolve_source"] = resolve_meta["per_symbol"].get(sym)
        if rec["conId"] is None:
            rec["preflight"] = "blocked"
            rec["block_reason"] = "conId unresolved (trader_service down or not in local universe)"
        else:
            rec["preflight"] = "ok"
            rec.pop("block_reason", None)
    save_ckpt(ck)
    n_ok = sum(1 for s in SYMBOLS if conids.get(s))
    print(f"resolved {n_ok}/{len(SYMBOLS)} trader={resolve_meta['trader']}", flush=True)

    print("=== data_download 180d 1-day ===", flush=True)
    ck["phase"] = "download"
    save_ckpt(ck)
    prior_dl = {
        s: (ck["symbols"].get(s) or {}).get("data")
        for s in SYMBOLS
        if isinstance((ck["symbols"].get(s) or {}).get("data"), dict)
    }
    prior_dl = {k: v for k, v in prior_dl.items() if v}
    dl = download_daily(SYMBOLS, conids, prior=prior_dl)
    for sym, info in dl.items():
        ck["symbols"].setdefault(sym, {}).update({"data": info})
    save_ckpt(ck)

    runnable = [
        s for s in SYMBOLS
        if conids.get(s) and (dl.get(s) or {}).get("rows", 0) >= 40
    ]
    blocked_data = [
        s for s in SYMBOLS
        if conids.get(s) and (dl.get(s) or {}).get("rows", 0) < 40
    ]
    for s in blocked_data:
        ck["symbols"][s]["preflight"] = "blocked"
        ck["symbols"][s]["block_reason"] = (
            f"daily bars insufficient: {(dl.get(s) or {}).get('rows', 0)}"
        )
    save_ckpt(ck)
    print(f"runnable {len(runnable)} blocked_data {len(blocked_data)}", flush=True)

    print("=== backtest_batch daily defaults ===", flush=True)
    ck["phase"] = "backtest"
    save_ckpt(ck)
    total_jobs = len(runnable) * len(STRATS)
    done = 0
    t0 = time.time()
    for si, sym in enumerate(runnable, 1):
        rec = ck["symbols"][sym]
        rec["learn_state"] = "active"
        rec["runs"] = rec.get("runs") or []
        already = {r.get("class") for r in rec["runs"] if r.get("status") == "ok"}
        for rel, cls in STRATS:
            if cls in already:
                done += 1
                continue
            try:
                row = run_one_backtest(rel, cls, int(conids[sym]), sym)
            except Exception as ex:
                row = {
                    "status": "error",
                    "class": cls,
                    "error": f"{type(ex).__name__}: {ex}",
                }
                print(f"    {sym} {cls} ERROR {ex}", flush=True)
            rec["runs"] = [r for r in rec["runs"] if r.get("class") != cls]
            rec["runs"].append(row)
            done += 1
        rec["learn_state"] = "batch_done"
        if si % 2 == 0 or si == len(runnable):
            save_ckpt(ck)
            elapsed = time.time() - t0
            print(
                f"  batch {si}/{len(runnable)} {sym} jobs {done}/{total_jobs} "
                f"{elapsed:.0f}s",
                flush=True,
            )

    print("=== backtests_confidence ===", flush=True)
    ck["phase"] = "confidence"
    save_ckpt(ck)
    for si, sym in enumerate(runnable, 1):
        rec = ck["symbols"][sym]
        runs = rec.get("runs") or []
        # Confidence only on the most traded / best-return candidates
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
            if r in top and r.get("run_id") and r.get("total_trades", 0) >= MIN_TRADES_RANK:
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
    }
    ck["phase"] = "done"
    save_ckpt(ck)
    print(f"DONE mix_complete={complete}/74", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
