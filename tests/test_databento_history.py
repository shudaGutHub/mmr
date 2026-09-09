import datetime as dt
from unittest.mock import MagicMock

import pandas as pd
import pytest
import pytz

from trader.listeners.databento_history import (
    DatabentoHistoryWorker,
    bars_from_databento_df,
)
from trader.objects import BarSize, WhatToShow


class TestBarSizeDatabentoSchema:
    def test_native_schemas(self):
        assert BarSize.to_databento_schema(BarSize.Secs1) == 'ohlcv-1s'
        assert BarSize.to_databento_schema(BarSize.Mins1) == 'ohlcv-1m'
        assert BarSize.to_databento_schema(BarSize.Hours1) == 'ohlcv-1h'
        assert BarSize.to_databento_schema(BarSize.Days1) == 'ohlcv-1d'

    def test_unsupported_raises(self):
        with pytest.raises(ValueError, match='unsupported BarSize'):
            BarSize.to_databento_schema(BarSize.Mins5)


class TestBarsFromDatabentoDf:
    def test_maps_tick_columns_and_eastern_index(self):
        idx = pd.DatetimeIndex(
            [pd.Timestamp('2026-07-10 13:30:00', tz='UTC')],
            name='ts_event',
        )
        raw = pd.DataFrame({
            'open': [100.0],
            'high': [101.0],
            'low': [99.0],
            'close': [100.5],
            'volume': [1_000],
            'symbol': ['QCOM'],
        }, index=idx)

        df = bars_from_databento_df(raw, BarSize.Mins1, timezone='US/Eastern')

        assert len(df) == 1
        assert df.index.name == 'date'
        assert str(df.index.tz) in ('US/Eastern', 'America/New_York')
        assert df.index[0].hour == 9  # 13:30 UTC -> 09:30 ET (EDT)
        assert df['open'].iloc[0] == 100.0
        assert df['close'].iloc[0] == 100.5
        assert df['average'].iloc[0] == pytest.approx((101.0 + 99.0 + 100.5) / 3.0)
        assert df['bar_count'].iloc[0] == 0
        assert df['bar_size'].iloc[0] == '1 min'
        assert df['what_to_show'].iloc[0] == int(WhatToShow.TRADES)
        assert df['symbol'].iloc[0] == 'QCOM'

    def test_converts_fixed_point_prices(self):
        idx = pd.DatetimeIndex([pd.Timestamp('2026-07-10 13:31:00', tz='UTC')])
        raw = pd.DataFrame({
            'open': [100_000_000_000],
            'high': [101_000_000_000],
            'low': [99_000_000_000],
            'close': [100_500_000_000],
            'volume': [10],
        }, index=idx)
        df = bars_from_databento_df(raw, BarSize.Mins1)
        assert df['open'].iloc[0] == pytest.approx(100.0)
        assert df['close'].iloc[0] == pytest.approx(100.5)

    def test_empty(self):
        df = bars_from_databento_df(pd.DataFrame(), BarSize.Mins1)
        assert df.empty


class TestDatabentoHistoryWorker:
    def test_requires_key(self, monkeypatch):
        monkeypatch.setattr(
            'trader.listeners.databento_history._ENV_FILES',
            (),
        )
        monkeypatch.delenv('DATABENTO_API_KEY', raising=False)
        monkeypatch.delenv('DATABENTO_KEY', raising=False)
        with pytest.raises(ValueError, match='DATABENTO_API_KEY'):
            DatabentoHistoryWorker(api_key='')

    def test_get_history_uses_first_dataset(self):
        idx = pd.DatetimeIndex(
            [pd.Timestamp('2026-07-10 13:30:00', tz='UTC')],
            name='ts_event',
        )
        raw = pd.DataFrame({
            'open': [10.0], 'high': [11.0], 'low': [9.0], 'close': [10.5],
            'volume': [100], 'symbol': ['AMD'],
        }, index=idx)
        store = MagicMock()
        store.to_df.return_value = raw
        client = MagicMock()
        client.timeseries.get_range.return_value = store

        worker = DatabentoHistoryWorker(
            api_key='db-test',
            datasets=('XNAS.ITCH',),
            historical_client=client,
        )
        df = worker.get_history(
            ticker='amd',
            bar_size=BarSize.Mins1,
            start_date=dt.datetime(2026, 7, 10),
            end_date=dt.datetime(2026, 9, 8),
            timezone='US/Eastern',
        )
        assert len(df) == 1
        assert 'symbol' not in df.columns
        client.timeseries.get_range.assert_called_once()
        kwargs = client.timeseries.get_range.call_args.kwargs
        assert kwargs['dataset'] == 'XNAS.ITCH'
        assert kwargs['schema'] == 'ohlcv-1m'
        assert kwargs['symbols'] == ['AMD']
        assert kwargs['start'] == '2026-07-10'
        assert kwargs['end'] == '2026-09-09'  # exclusive bump from date-only end

    def test_falls_back_on_entitlement_error(self):
        idx = pd.DatetimeIndex(
            [pd.Timestamp('2026-07-10 13:30:00', tz='UTC')],
            name='ts_event',
        )
        raw = pd.DataFrame({
            'open': [10.0], 'high': [11.0], 'low': [9.0], 'close': [10.5],
            'volume': [100], 'symbol': ['SHOP'],
        }, index=idx)
        store = MagicMock()
        store.to_df.return_value = raw

        forbidden = RuntimeError('403 Forbidden: dataset not permissioned')
        forbidden.http_status = 403  # type: ignore[attr-defined]

        client = MagicMock()
        client.timeseries.get_range.side_effect = [forbidden, store]

        worker = DatabentoHistoryWorker(
            api_key='db-test',
            datasets=('XNAS.ITCH', 'XNAS.BASIC'),
            historical_client=client,
        )
        many = worker.get_history_many(
            ['SHOP'],
            BarSize.Mins1,
            dt.datetime(2026, 7, 10),
            dt.datetime(2026, 7, 11, 16, 0, 0),
        )
        assert 'SHOP' in many
        assert len(many['SHOP']) == 1
        assert client.timeseries.get_range.call_count == 2
        datasets = [c.kwargs['dataset'] for c in client.timeseries.get_range.call_args_list]
        assert datasets == ['XNAS.ITCH', 'XNAS.BASIC']
