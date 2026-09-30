"""
Launcher for TrendScope.

The dashboard is the whole product: searching a topic in ui/app.py plans the search,
runs the collectors and refreshes the page (see engine.py). There is no separate
background process to start any more -- this file only exists so that

    python main.py

opens the app, exactly like `streamlit run ui/app.py`. Any extra arguments are
passed through to Streamlit (e.g. `python main.py --server.port 8600`).
"""

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_PATH = os.path.join(PROJECT_ROOT, "ui", "app.py")


def main():
    command = [sys.executable, "-m", "streamlit", "run", APP_PATH, *sys.argv[1:]]
    try:
        return subprocess.call(command, cwd=PROJECT_ROOT)
    except KeyboardInterrupt:
        print("\n👋 TrendScope stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
