"""Indian market data layer (FYERS adapter, instruments, sessions, aggregation)."""
from .instruments import (
    Exchange,
    InstrumentType,
    Instrument,
    InternalSymbol,
    InstrumentRegistry,
    DEFAULT_INSTRUMENTS,
)
from .symbol_map import to_fyers_symbol, from_fyers_symbol
from .market_calendar import (
    KOLKATA,
    TradingCalendar,
    SessionPhase,
    is_trading_day,
    market_state,
    is_within_session,
    session_boundaries,
    next_session_open,
    session_phase,
)
from .candle_aggregator import CandleAggregator, AggregatedBar, timeframe_minutes
from .events import InternalMarketEvent, EventType
from .fyers import FYERSMarketDataProvider, FyersDataSocket
from .history_chunking import (
    ChunkedHistoricalFetcher,
    plan_chunks,
    combine_frames,
    DateChunk,
)
from .instrument_repository import InstrumentRepository
from .event_bus import EventBus, EventConsumer
from .closed_candle_pipeline import (
    ClosedCandlePipeline,
    ClosedCandle,
    CandleState,
)
from .data_health import DataHealthMonitor, FeedStatus

__all__ = [
    "Exchange",
    "InstrumentType",
    "Instrument",
    "InternalSymbol",
    "InstrumentRegistry",
    "DEFAULT_INSTRUMENTS",
    "to_fyers_symbol",
    "from_fyers_symbol",
    "KOLKATA",
    "TradingCalendar",
    "SessionPhase",
    "is_trading_day",
    "market_state",
    "is_within_session",
    "session_boundaries",
    "next_session_open",
    "session_phase",
    "CandleAggregator",
    "AggregatedBar",
    "timeframe_minutes",
    "InternalMarketEvent",
    "EventType",
    "FYERSMarketDataProvider",
    "FyersDataSocket",
    "ChunkedHistoricalFetcher",
    "plan_chunks",
    "combine_frames",
    "DateChunk",
    "InstrumentRepository",
    "EventBus",
    "EventConsumer",
    "ClosedCandlePipeline",
    "ClosedCandle",
    "CandleState",
    "DataHealthMonitor",
    "FeedStatus",
]
