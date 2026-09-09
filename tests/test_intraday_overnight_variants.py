"""EOD-flat 1-min variants of MeanReversion, Momentum, and KeltnerBreakout.

Overnight originals stay as daily/swing books. These variants must:
  - declare flatten-by 15:45 ET
  - attach close_by_time on BUY entries
  - refuse new entries after 15:00 ET
  - pass the precompute lookahead audit
"""

from datetime import time as dtime

import numpy as np
import pandas as pd

from trader.objects import Action
from trader.simulation.lookahead_check import assert_no_lookahead
from trader.trading.strategy import Signal


def _et_session(n: int = 390, start: str = "2026-04-20 09:30") -> pd.DataFrame:
    """Regular-hours 1-min bars in America/New_York."""
    idx = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    close = np.full(n, 100.0)
    df = pd.DataFrame({
        "open": close,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": np.full(n, 1000.0),
    }, index=idx)
    df.index.name = "date"
    return df


def _walk(strategy, prices: pd.DataFrame) -> list[tuple[int, Signal]]:
    state = strategy.precompute(prices)
    out = []
    for i in range(len(prices)):
        sig = strategy.on_bar(prices, state, i)
        if sig is not None:
            out.append((i, sig))
    return out


def _bar_at(prices: pd.DataFrame, hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    et = prices.index.tz_convert("America/New_York")
    for i, ts in enumerate(et):
        if ts.hour == hour and ts.minute == minute:
            return i
    raise AssertionError(f"no bar at {hhmm} ET")


class TestOvernightBooksUnchanged:
    def test_mean_reversion_buy_has_no_time_exit(self):
        from strategies.mean_reversion import MeanReversion

        idx = pd.date_range("2024-01-02", periods=40, freq="B")
        close = np.full(40, 100.0)
        close[-1] = 90.0
        prices = pd.DataFrame({
            "open": close, "high": close + 1, "low": np.minimum(close, 90.0),
            "close": close, "volume": np.full(40, 1e6),
        }, index=idx)
        strat = MeanReversion()
        sig = strat.on_prices(prices)
        assert sig is not None
        assert sig.action == Action.BUY
        assert sig.close_by_time is None
        assert sig.max_hold_bars is None

    def test_keltner_breakout_has_no_eod_attrs(self):
        from strategies.keltner_breakout import KeltnerBreakout

        assert not hasattr(KeltnerBreakout, "EOD_HOUR")
        assert not hasattr(KeltnerBreakout, "EOD_MINUTE")


class TestIntradayEodFlattenDeclared:
    def test_variants_flatten_at_1545(self):
        from strategies.keltner_breakout_intraday import KeltnerBreakoutIntraday
        from strategies.mean_reversion_intraday import MeanReversionIntraday
        from strategies.momentum_intraday import MomentumIntraday

        for cls in (MeanReversionIntraday, MomentumIntraday, KeltnerBreakoutIntraday):
            assert cls.EOD_HOUR == 15
            assert cls.EOD_MINUTE == 45


class TestMeanReversionIntraday:
    def test_buy_carries_close_by_time_and_fires_in_rth(self):
        from strategies.mean_reversion_intraday import MeanReversionIntraday

        prices = _et_session()
        dump = _bar_at(prices, "10:30")
        # Tight 100s then a crash through the lower band.
        prices.loc[prices.index[:dump], ["open", "high", "low", "close"]] = 100.0
        prices.loc[prices.index[dump], ["open", "close", "low"]] = [100.0, 92.0, 91.0]
        prices.loc[prices.index[dump], "high"] = 100.0

        signals = _walk(MeanReversionIntraday(), prices)
        buys = [(i, s) for i, s in signals if s.action == Action.BUY]
        assert buys, "expected a lower-band BUY after the 10:30 dump"
        idx, sig = buys[0]
        assert prices.index[idx].tz_convert("America/New_York").hour == 10
        assert sig.close_by_time == dtime(15, 45)

    def test_no_new_buy_after_1500(self):
        from strategies.mean_reversion_intraday import MeanReversionIntraday

        prices = _et_session()
        dump = _bar_at(prices, "15:10")
        prices.loc[prices.index[:dump], ["open", "high", "low", "close"]] = 100.0
        prices.loc[prices.index[dump], ["open", "close", "low"]] = [100.0, 92.0, 91.0]
        prices.loc[prices.index[dump], "high"] = 100.0

        signals = _walk(MeanReversionIntraday(), prices)
        buys = [s for _, s in signals if s.action == Action.BUY]
        assert buys == []

    def test_no_lookahead(self):
        from strategies.mean_reversion_intraday import MeanReversionIntraday

        rng = np.random.default_rng(0)
        prices = _et_session(250)
        prices["close"] = 100.0 + np.cumsum(rng.normal(0, 0.15, len(prices)))
        prices["open"] = prices["close"]
        prices["high"] = prices["close"] + 0.2
        prices["low"] = prices["close"] - 0.2
        assert_no_lookahead(MeanReversionIntraday(), prices)


class TestMomentumIntraday:
    def test_buy_carries_close_by_time(self):
        from strategies.momentum_intraday import MomentumIntraday

        prices = _et_session()
        start = _bar_at(prices, "10:30")
        end = _bar_at(prices, "10:50")
        # Flat, then a 15-bar lift that crosses the ROC threshold.
        for i in range(start, end + 1):
            px = 100.0 + (i - start) * 0.08
            prices.iloc[i, prices.columns.get_loc("close")] = px
            prices.iloc[i, prices.columns.get_loc("open")] = px
            prices.iloc[i, prices.columns.get_loc("high")] = px + 0.05
            prices.iloc[i, prices.columns.get_loc("low")] = px - 0.05
            prices.iloc[i, prices.columns.get_loc("volume")] = 5000.0

        signals = _walk(MomentumIntraday(), prices)
        buys = [(i, s) for i, s in signals if s.action == Action.BUY]
        assert buys, "expected ROC up-cross BUY during the 10:30 lift"
        _, sig = buys[0]
        assert sig.close_by_time == dtime(15, 45)

    def test_no_new_buy_after_1500(self):
        from strategies.momentum_intraday import MomentumIntraday

        prices = _et_session()
        start = _bar_at(prices, "15:00")
        end = min(start + 20, len(prices) - 1)
        for i in range(start, end + 1):
            px = 100.0 + (i - start) * 0.08
            prices.iloc[i, prices.columns.get_loc("close")] = px
            prices.iloc[i, prices.columns.get_loc("open")] = px
            prices.iloc[i, prices.columns.get_loc("high")] = px + 0.05
            prices.iloc[i, prices.columns.get_loc("low")] = px - 0.05
            prices.iloc[i, prices.columns.get_loc("volume")] = 5000.0

        signals = _walk(MomentumIntraday(), prices)
        buys = [s for _, s in signals if s.action == Action.BUY]
        assert buys == []

    def test_no_lookahead(self):
        from strategies.momentum_intraday import MomentumIntraday

        rng = np.random.default_rng(1)
        prices = _et_session(250)
        prices["close"] = 100.0 + np.cumsum(rng.normal(0, 0.12, len(prices)))
        prices["open"] = prices["close"]
        prices["high"] = prices["close"] + 0.2
        prices["low"] = prices["close"] - 0.2
        prices["volume"] = rng.integers(800, 4000, len(prices)).astype(float)
        assert_no_lookahead(MomentumIntraday(), prices)


class TestKeltnerBreakoutIntraday:
    def test_buy_carries_close_by_time(self):
        from strategies.keltner_breakout_intraday import KeltnerBreakoutIntraday

        prices = _et_session()
        spike = _bar_at(prices, "10:45")
        prices.loc[prices.index[spike], "close"] = 108.0
        prices.loc[prices.index[spike], "high"] = 108.5
        prices.loc[prices.index[spike], "open"] = 100.5
        prices.loc[prices.index[spike], "volume"] = 20_000.0

        signals = _walk(KeltnerBreakoutIntraday(), prices)
        buys = [(i, s) for i, s in signals if s.action == Action.BUY]
        assert buys, "expected upper-band breakout BUY at the 10:45 spike"
        _, sig = buys[0]
        assert sig.close_by_time == dtime(15, 45)

    def test_no_new_buy_after_1500(self):
        from strategies.keltner_breakout_intraday import KeltnerBreakoutIntraday

        prices = _et_session()
        spike = _bar_at(prices, "15:10")
        prices.loc[prices.index[spike], "close"] = 108.0
        prices.loc[prices.index[spike], "high"] = 108.5
        prices.loc[prices.index[spike], "volume"] = 20_000.0

        signals = _walk(KeltnerBreakoutIntraday(), prices)
        buys = [s for _, s in signals if s.action == Action.BUY]
        assert buys == []

    def test_no_lookahead(self):
        from strategies.keltner_breakout_intraday import KeltnerBreakoutIntraday

        rng = np.random.default_rng(2)
        prices = _et_session(250)
        prices["close"] = 100.0 + np.cumsum(rng.normal(0, 0.2, len(prices)))
        prices["open"] = prices["close"]
        prices["high"] = prices["close"] + 0.4
        prices["low"] = prices["close"] - 0.4
        prices["volume"] = rng.integers(800, 5000, len(prices)).astype(float)
        assert_no_lookahead(KeltnerBreakoutIntraday(), prices)
