"""Intraday Bollinger mean reversion — same thesis as MeanReversion, 1-min RTH.

Overnight MeanReversion holds through the close. This variant:

  - Uses a 20-bar (20-minute) Bollinger window on 1-min bars.
  - Only enters during 09:45–15:00 ET.
  - BUY when close crosses below the lower band; SELL when close crosses
    back above the mid-band (reversion done) or the upper band.
  - Every BUY carries close_by_time 15:45 ET so the book is flat EOD.

precompute + on_bar so a 60-day 1-min replay stays O(N).
"""

from datetime import time as dtime
from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


class MeanReversionIntraday(Strategy):
    """20-bar Bollinger mean reversion, RTH-only, flat by 15:45 ET."""

    WINDOW = 20
    NUM_STD = 2.0
    ENTRY_START_MIN = 9 * 60 + 45
    ENTRY_END_MIN = 15 * 60
    RTH_OPEN_MIN = 9 * 60 + 30
    RTH_CLOSE_MIN = 16 * 60
    EOD_HOUR = 15
    EOD_MINUTE = 45
    MIN_BARS = 30

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert("America/New_York")
        et_minute = (et.hour * 60 + et.minute).to_numpy()

        close = prices["close"]
        sma = close.rolling(self.WINDOW).mean()
        std = close.rolling(self.WINDOW).std()
        upper = sma + self.NUM_STD * std
        lower = sma - self.NUM_STD * std

        return {
            "close": close.to_numpy(),
            "sma": sma.to_numpy(),
            "upper": upper.to_numpy(),
            "lower": lower.to_numpy(),
            "entry_window": (
                (et_minute >= self.ENTRY_START_MIN) & (et_minute < self.ENTRY_END_MIN)
            ),
            "rth": (
                (et_minute >= self.RTH_OPEN_MIN) & (et_minute < self.RTH_CLOSE_MIN)
            ),
        }

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        if not state or index < self.MIN_BARS:
            return None
        if not state["rth"][index]:
            return None

        close = state["close"][index]
        prev_close = state["close"][index - 1]
        sma = state["sma"][index]
        lower = state["lower"][index]
        prev_lower = state["lower"][index - 1]
        upper = state["upper"][index]
        prev_upper = state["upper"][index - 1]
        prev_sma = state["sma"][index - 1]

        if np.isnan(lower) or np.isnan(prev_lower) or np.isnan(sma) or np.isnan(upper):
            return None

        eod = dtime(self.EOD_HOUR, self.EOD_MINUTE)

        if state["entry_window"][index]:
            if close < lower and prev_close >= prev_lower:
                return Signal(
                    source_name=self.name, action=Action.BUY,
                    probability=0.65, risk=0.35,
                    close_by_time=eod,
                )

        # Exits stay available after the entry cutoff so a morning long
        # can still flatten at the mid-band before the 15:45 time stop.
        if close > sma and prev_close <= prev_sma:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.60, risk=0.40,
            )
        if close > upper and prev_close <= prev_upper:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.65, risk=0.35,
            )
        return None
