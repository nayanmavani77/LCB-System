"""Central place for output paths. Everything the system GENERATES goes to
logs/ (gitignored): MT5 signal logs, paper trade logs, state snapshots,
exported backtest CSVs."""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(REPO, "logs")


def log_path(filename: str) -> str:
    """Return logs/<filename>, creating logs/ if needed. Absolute paths and
    paths that already contain a directory are returned unchanged."""
    if os.path.isabs(filename) or os.path.dirname(filename):
        return filename
    os.makedirs(LOGS, exist_ok=True)
    return os.path.join(LOGS, filename)
