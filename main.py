"""
Launcher for TrendAnalyzer Pro.

Starts the Streamlit dashboard (ui/app.py) as a child process. Each launch begins
from an empty corpus: the SQLite database and the persisted topic plan are deleted
first, so the dashboard never presents data from a previous session. Extra CLI
arguments are forwarded to Streamlit, e.g. `python main.py --server.port 8600`.
"""

import os
import subprocess
import sys

from config import DATA_DIR, DB_PATH, PROFILE_PATH, PROJECT_ROOT

APP_PATH = os.path.join(PROJECT_ROOT, "ui", "app.py")


def main():
    """
    Resets the corpus and runs the dashboard until the user stops it.

    Returns:
        int: The Streamlit exit code, or 0 after a Ctrl+C interrupt.
    """
    # Reset both together: the plan labels the corpus, so deleting only one would
    # leave the two inconsistent.
    # The -wal/-shm files belong to the database in WAL mode and go with it.
    for path in (DB_PATH, f"{DB_PATH}-wal", f"{DB_PATH}-shm", PROFILE_PATH):
        if os.path.exists(path):
            os.remove(path)
    os.makedirs(DATA_DIR, exist_ok=True)

    # Launch marker inherited by the child; ui/app.py refuses to run without it, which
    # guarantees the reset above always precedes the dashboard.
    os.environ["TREND_APP_LAUNCHED"] = "1"
    # sys.executable pins Streamlit to this interpreter (and its virtualenv).
    command = [sys.executable, "-m", "streamlit", "run", APP_PATH, *sys.argv[1:]]
    try:
        return subprocess.call(command, cwd=PROJECT_ROOT)
    except KeyboardInterrupt:
        print("\nTrendAnalyzer Pro stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
