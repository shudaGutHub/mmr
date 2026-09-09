"""Intraday ROC momentum — same thesis as Momentum, 1-min RTH, flat EOD.

Overnight Momentum uses a 10-bar / 5% ROC (daily-scale). This variant
recalibrates to 1-min:

  - 15-bar (15-minute) ROC, 0.35% threshold.
  - Volume must exceed 0.8× the 20-bar average.
  - Entries only 09:45–15:00 ET.
  - BUY carries close_by_time 15:45 ET.

SELL fires when ROC crosses below zero (impulse spent) so a long does
not wait until the opposite 0.35% dump — the time stop is the backstop.
"""

from datetime import time as dtime
from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


class MomentumIntraday(Strategy):
    """15-minute ROC momentum with volume confirmation, RTH-only, flat by 15:45 ET."""

    ROC_PERIOD = 15
    ROC_THRESHOLD = 0.35          # percent; 5.0 was the daily-bar default
    VOL_AVG_PERIOD = 20
    VOLUME_MULT = 0.8
    ENTRY_START_MIN = 9 * 60 + 45
    ENTRY_END_MIN = 15 * 60
    RTH_OPEN_MIN = 9 * 60 + 30
    RTH_CLOSE_MIN = 16 * 60
    EOD_HOUR = 15
    EOD_MINUTE = 45
    MIN_BARS = 40

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert("America/New_York")
        et_minute = (et.hour * 60 + et.minute).to_numpy()

        close = prices["close"]
        roc = close.pct_change(periods=self.ROC_PERIOD) * 100.0
        vol_avg = prices["volume"].rolling(self.VOL_AVG_PERIOD).mean()

        return {
            "close": close.to_numpy(),
            "roc": roc.to_numpy(),
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

        roc = state["roc"][index]
        prev_roc = state["roc"][index - 1]
        vol_avg = state["vol_avg"][index]
        volume = state["volume"][index]

        if np.isnan(roc) or np.isnan(prev_roc) or np.isnan(vol_avg) or vol_avg <= 0:
            return None

        eod = dtime(self.EOD_HOUR, self.EOD_MINUTE)
        vol_ok = volume > vol_avg * self.VOLUME_MULT

        if state["entry_window"][index] and vol_ok:
            if roc > self.ROC_THRESHOLD and prev_roc <= self.ROC_THRESHOLD:
                return Signal(
                    source_name=self.name, action=Action.BUY,
                    probability=0.55, risk=0.45,
                    close_by_time=eod,
                )

        # Impulse spent — flatten the long. Opposite-side short is rejected
        # by the long-only backtester if we are already flat.
        if roc < 0.0 and prev_roc >= 0.0:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.55, risk=0.45,
            )
        if roc < -self.ROC_THRESHOLD and prev_roc >= -self.ROC_THRESHOLD:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.55, risk=0.45,
            )
        return None
