"""
The dashboard container's entry point: python -m dashboard.serve [streamlit options]

Streamlit runs a page's script only when someone opens a page, so a metrics server started from a page (as
dashboard/lib/page_setup.py also does, for a run outside the container) does not exist until the first visitor
arrives. Prometheus would report an idle dashboard as down, and ScrapeTargetDown would fire. This starts the
metrics server with the process, then runs Streamlit in the same process, so the scripts' metrics are the ones served.
"""
from __future__ import annotations

import sys

from src.config import settings
from src.metrics import start_metrics_server

APP_SCRIPT = "dashboard/app.py"


def main(argv: list[str] | None = None) -> int:
    start_metrics_server(port=settings.METRICS_PORT)
    from streamlit.web import cli

    sys.argv = ["streamlit", "run", APP_SCRIPT, *(sys.argv[1:] if argv is None else argv)]
    return cli.main() or 0


if __name__ == "__main__":
    sys.exit(main())
