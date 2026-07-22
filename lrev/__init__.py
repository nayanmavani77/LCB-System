"""L-Rev v2: single source of truth for backtest AND live trading."""
from .strategy import LRevStrategy, Bar, TF_SECONDS, DEFAULT_CONFIG
from .broker import Broker, PaperBroker

