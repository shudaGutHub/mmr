"""Intraday Keltner breakout — same bands as KeltnerBreakout, RTH, flat EOD.

Overnight KeltnerBreakout can hold through the close. This copy keeps
EMA(20) ± BAND_MULT×ATR(10) and the volume gate, then:

  - Ignores pre/post-market bars.
  - Refuses new BUYs after 15:00 ET.
  - Stamps close_by_time 15:45 ET on every BUY.
"""

from datetime import time as dtime
from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


class KeltnerBreakoutIntraday(Strategy):
    """EMA(20) ± 2×ATR(10) breakout, RTH-only, flat by 15:45 ET."""

    EMA_PERIOD = 20
    ATR_PERIOD = 10
    BAND_MULT = 2.0
    VOLUME_MULT = 1.3
    MIN_BARS = 40
    ENTRY_START_MIN = 9 * 60 + 45
    ENTRY_END_MIN = 15 * 60
    RTH_OPEN_MIN = 9 * 60 + 30
    RTH_CLOSE_MIN = 16 * 60
    EOD_HOUR = 15
    EOD_MINUTE = 45

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert("America/New_York")
        et_minute = (et.hour * 60 + et.minute).to_numpy()

        close = prices["close"]
        high = prices["high"]
        low = prices["low"]
        prev_close = close.shift(1)

        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.ATR_PERIOD).mean()

        ema = close.ewm(span=self.EMA_PERIOD, adjust=False).mean()
        upper = ema + self.BAND_MULT * atr
        lower = ema - self.BAND_MULT * atr
        vol_avg = prices["volume"].rolling(20).mean()

        return {
            "close": close.to_numpy(),
            "upper": upper.to_numpy(),
            "lower": lower.to_numpy(),
            "volume": prices["volume"].to_numpy(),
            "vol_avg": vol_avg.to_numpy(),
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
        upper = state["upper"][index]
        prev_upper = state["upper"][index - 1]
        lower = state["lower"][index]
        prev_lower = state["lower"][index - 1]
        volume = state["volume"][index]
        vol_avg = state["vol_avg"][index]

        if (np.isnan(upper) or np.isnan(lower) or np.isnan(prev_upper)
                or np.isnan(prev_lower) or np.isnan(vol_avg) or vol_avg <= 0):
            return None

        vol_ok = volume > vol_avg * self.VOLUME_MULT
        eod = dtime(self.EOD_HOUR, self.EOD_MINUTE)

        if state["entry_window"][index] and vol_ok:
            if close > upper and prev_close <= prev_upper:
                return Signal(
                    source_name=self.name, action=Action.BUY,
                    probability=0.60, risk=0.40,
                    close_by_time=eod,
                )
        if close < lower and prev_close >= prev_lower:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.60, risk=0.40,
            )
        return None
