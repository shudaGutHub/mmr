from trader.common.logging_helper import setup_logging
from trader.objects import BarSize, WhatToShow

import datetime as dt
import os
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import pytz


logging = setup_logging(module_name='databento_history')

# NASDAQ Basic first (no live TotalView license). ITCH is tried next for
# names BASIC misses; DBEQ covers dual-listed / consolidated.
DEFAULT_DATASETS: tuple[str, ...] = ('XNAS.BASIC', 'XNAS.ITCH', 'DBEQ.BASIC')

_ENV_FILES = (
    Path.home() / '.config' / 'mmr' / 'secrets.env',
    Path(r'c:\Users\salee\BITBUCKET\databento-relay\databento-relay\.env'),
)


def load_databento_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip().strip('"').strip("'")
    for name in ('DATABENTO_API_KEY', 'DATABENTO_KEY'):
        raw = os.environ.get(name)
        if raw:
            return raw.strip().strip('"').strip("'")
    for path in _ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            k, v = stripped.split('=', 1)
            if k.strip() in ('DATABENTO_API_KEY', 'DATABENTO_KEY'):
                return v.strip().strip('"').strip("'")
    return ''


def _is_entitlement_error(ex: Exception) -> bool:
    msg = str(ex).lower()
    status = getattr(ex, 'http_status', None) or getattr(ex, 'status_code', None)
    if status in (401, 403):
        return True
    needles = (
        'not permissioned',
        'not entitled',
        'authorization',
        'dataset_not_found',
        'forbidden',
        'unauthenticated',
    )
    return any(n in msg for n in needles)


def _as_float_px(value) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return float('nan')
    num = float(value)
    # Databento fixed-point is 1e-9. to_df(price_type='float') already converts;
    # this guard covers callers that pass raw DBN ints.
    if abs(num) >= 1e6:
        return num / 1e9
    return num


def bars_from_databento_df(
    raw: pd.DataFrame,
    bar_size: BarSize,
    timezone: str = 'US/Eastern',
) -> pd.DataFrame:
    """Map a Databento ohlcv DataFrame onto TickData columns.

    Index becomes tz-aware `date` in `timezone`. No VWAP on ohlcv schemas —
    `average` is typical price (H+L+C)/3. `bar_count` is 0 when the schema
    has no trade-count field.
    """
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    if 'ts_event' in df.columns and not isinstance(df.index, pd.DatetimeIndex):
        df = df.set_index('ts_event')
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)

    tz = pytz.timezone(timezone)
    if df.index.tz is None:
        df.index = df.index.tz_localize(pytz.UTC)
    df.index = df.index.tz_convert(tz)
    df.index.name = 'date'

    required = ('open', 'high', 'low', 'close')
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f'Databento OHLCV missing columns: {missing}')

    volume = df['volume'] if 'volume' in df.columns else 0
    bar_count = df['bar_count'] if 'bar_count' in df.columns else 0
    if 'average' in df.columns:
        average = df['average'].map(_as_float_px)
    else:
        average = (
            df['high'].map(_as_float_px)
            + df['low'].map(_as_float_px)
            + df['close'].map(_as_float_px)
        ) / 3.0

    out = pd.DataFrame({
        'open': df['open'].map(_as_float_px),
        'high': df['high'].map(_as_float_px),
        'low': df['low'].map(_as_float_px),
        'close': df['close'].map(_as_float_px),
        'volume': volume,
        'average': average,
        'bar_count': bar_count,
        'bar_size': str(bar_size),
        'what_to_show': int(WhatToShow.TRADES),
    }, index=df.index)
    if 'symbol' in df.columns:
        out['symbol'] = df['symbol'].astype(str)

    out = out[~out.index.duplicated(keep='first')]
    out.sort_index(ascending=True, inplace=True)
    return out


def _split_by_symbol(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if df.empty:
        return {}
    if 'symbol' not in df.columns:
        return {'': df}
    out: dict[str, pd.DataFrame] = {}
    for sym, part in df.groupby(df['symbol'].astype(str).str.upper(), sort=False):
        part = part.drop(columns=['symbol'])
        out[str(sym)] = part
    return out


class DatabentoHistoryWorker:
    def __init__(
        self,
        api_key: str,
        datasets: Sequence[str] | None = None,
        historical_client=None,
    ):
        if historical_client is not None:
            self.client = historical_client
        else:
            key = load_databento_api_key(api_key)
            if not key:
                raise ValueError('DATABENTO_API_KEY is required')
            import databento as db
            self.client = db.Historical(key=key)
        self.datasets = tuple(datasets) if datasets else DEFAULT_DATASETS

    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        many = self.get_history_many(
            [ticker], bar_size, start_date, end_date, timezone=timezone,
        )
        return many.get(ticker.upper(), pd.DataFrame())

    def get_history_many(
        self,
        tickers: Iterable[str],
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> dict[str, pd.DataFrame]:
        symbols = [s.upper() for s in tickers]
        if not symbols:
            return {}
        schema = BarSize.to_databento_schema(bar_size)
        start = start_date.strftime('%Y-%m-%d')
        # Databento `end` is exclusive. Date-only strings drop the clock, so
        # bump one calendar day, but never past the current UTC date — some
        # datasets 422 if `end` is after available_end.
        end_d = (end_date + dt.timedelta(days=1)).date()
        utc_today = dt.datetime.now(dt.timezone.utc).date()
        if end_d > utc_today:
            end_d = utc_today
        end = end_d.isoformat()

        remaining = list(symbols)
        collected: dict[str, pd.DataFrame] = {}
        last_error: Exception | None = None

        for dataset in self.datasets:
            if not remaining:
                break
            try:
                logging.info('get_history_many {} {} {} {} to {}'.format(
                    dataset, schema, remaining, start, end,
                ))
                store = self.client.timeseries.get_range(
                    dataset=dataset,
                    schema=schema,
                    symbols=remaining,
                    start=start,
                    end=end,
                    stype_in='raw_symbol',
                )
                raw = store.to_df(price_type='float', pretty_ts=True, map_symbols=True, tz=timezone)
            except Exception as ex:
                last_error = ex
                if _is_entitlement_error(ex):
                    logging.warning('databento dataset {} not entitled: {}'.format(dataset, ex))
                    continue
                logging.warning('databento get_range {} failed: {}'.format(dataset, ex))
                continue

            mapped = bars_from_databento_df(raw, bar_size, timezone=timezone)
            parts = _split_by_symbol(mapped)
            if list(parts.keys()) == [''] and len(remaining) == 1:
                parts = {remaining[0]: parts['']}
            still: list[str] = []
            for sym in remaining:
                part = parts.get(sym, pd.DataFrame())
                if part is None or part.empty:
                    still.append(sym)
                    continue
                collected[sym] = part
                logging.info('get_history returned {} rows for {} via {}'.format(
                    len(part), sym, dataset,
                ))
            remaining = still

        if remaining and last_error and not collected:
            raise last_error
        for sym in remaining:
            collected.setdefault(sym, pd.DataFrame())
            logging.info('no data returned for {} from {} to {}'.format(sym, start, end))
        return collected
