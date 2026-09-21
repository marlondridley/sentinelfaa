"""Shared library code for the NAS FeedOps Sentinel Streamlit dashboard.

Every page under dashboard/pages/, plus dashboard/app.py itself, imports
from here rather than duplicating logic -- this package is what makes the
multipage split a real decomposition instead of six copies of the same
helpers. See page_setup.py, data.py, and ticker.py for what each covers.
"""
