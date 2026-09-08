"""RSI + ATR intraday range trading.

Thesis: Large-cap names spend most sessions oscillating in a range.
When 5-min RSI dips below oversold and then turns back up (cross from
below), price tends to revert toward the middle of the intraday range.
ATR provides the trade geometry: profit target and stop are both
expressed in multiples of the current ATR, so trades self-scale to
each name's volatility.

Rules (long-only — the backtester rejects shorts):
  Entry:  RSI(14) crosses UP through RSI_OVERSOLD, during RTH entry
          window (9:45–15:00 ET by default).
  Exit:   close >= entry + TP_ATR_MULT × ATR  (target), or
          close <= entry - SL_ATR_MULT × ATR  (stop), or
          RSI >= RSI_EXIT (mean reversion done), or
          15:55 ET EOD flat / MAX_HOLD_BARS backstop.

Data note: Massive intraday bars include pre/post-market; all entries
are gated to regular trading hours via ET time-of-day masks.
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional
from datetime import time as dtime

import numpy as np
import pandas as pd


class RsiAtrRange(Strategy):
    """Buy 5-min RSI oversold upcrosses; exit at ATR target/stop or RSI recovery."""

    RSI_PERIOD = 14
    RSI_OVERSOLD = 30
    RSI_EXIT = 60
    ATR_PERIOD = 14
    TP_ATR_MULT = 1.5
    SL_ATR_MULT = 1.0
    ENTRY_START_MIN = 9 * 60 + 45   # 9:45 ET — let indicators settle after open
    ENTRY_END_MIN = 15 * 60         # no new entries after 15:00 ET
    EOD_FLAT_MIN = 15 * 60 + 55     # flat by 15:55 ET
    MAX_HOLD_BARS = 36              # 3 hours on 5-min bars
    MIN_BARS = 30

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert("America/New_York")
        et_minute = (et.hour * 60 + et.minute).to_numpy()

        entry_window = (et_minute >= self.ENTRY_START_MIN) & (et_minute < self.ENTRY_END_MIN)
        rth = (et_minute >= 9 * 60 + 30) & (et_minute < 16 * 60)
        eod = et_minute >= self.EOD_FLAT_MIN

        close = prices["close"]

        # RSI (simple rolling mean of gains/losses)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self.RSI_PERIOD).mean()
        loss = (-delta.clip(upper=0)).rolling(self.RSI_PERIOD).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100.0 - (100.0 / (1.0 + rs))).to_numpy()

        # ATR (rolling mean of true range)
        tr1 = prices["high"] - prices["low"]
        tr2 = (prices["high"] - close.shift()).abs()
        tr3 = (prices["low"] - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(self.ATR_PERIOD).mean().to_numpy()

        return {
            "close": close.to_numpy(),
            "rsi": rsi,
            "atr": atr,
            "entry_window": entry_window,
            "rth": rth,
            "eod": eod,
            # Mutable per-conid position tracker (precompute state is
            # per-conid, so this doesn't leak across symbols).
            "pos": {"active": False, "entry": np.nan, "atr_entry": np.nan},
        }

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        if not state or index < self.MIN_BARS:
            return None

        close = state["close"][index]
        rsi = state["rsi"][index]
        rsi_prev = state["rsi"][index - 1]
        atr = state["atr"][index]
        pos = state["pos"]

        # ----- exit management for an open (tracked) position -----
        if pos["active"]:
            exit_now = False
            if state["eod"][index]:
                exit_now = True
            elif not np.isnan(pos["atr_entry"]):
                if close >= pos["entry"] + self.TP_ATR_MULT * pos["atr_entry"]:
                    exit_now = True   # ATR profit target
                elif close <= pos["entry"] - self.SL_ATR_MULT * pos["atr_entry"]:
                    exit_now = True   # ATR stop
            if not exit_now and not np.isnan(rsi) and rsi >= self.RSI_EXIT:
                exit_now = True       # reversion complete
            if exit_now:
                pos["active"] = False
                pos["entry"] = np.nan
                pos["atr_entry"] = np.nan
                return Signal(
                    source_name=self.name, action=Action.SELL,
                    probability=0.6, risk=0.4,
                )
            return None

        # ----- entry: RSI upcross through oversold, inside RTH window -----
        if not state["entry_window"][index]:
            return None
        if np.isnan(rsi) or np.isnan(rsi_prev) or np.isnan(atr) or atr <= 0:
            return None
        if not (rsi_prev < self.RSI_OVERSOLD and rsi >= self.RSI_OVERSOLD):
            return None

        pos["active"] = True
        pos["entry"] = close          # approximation — actual fill is next bar's open
        pos["atr_entry"] = atr

        return Signal(
            source_name=self.name, action=Action.BUY,
            probability=0.6, risk=0.4,
            max_hold_bars=self.MAX_HOLD_BARS,
            close_by_time=dtime(15, 55),
        )
