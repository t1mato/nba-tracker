"""NBA analytics dashboard.

    streamlit run src/dashboard/app.py

Reads the star schema built by the transform layer. Nothing here queries the
staging tables or the API directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit puts the script's own directory on sys.path, not the project root,
# so `from src.dashboard import ...` needs this before any local import.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(
    page_title="NBA Analytics",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)

pages = [
    st.Page("pages/team_trends.py", title="Team trends", icon="📈", default=True),
    st.Page("pages/player_deep_dive.py", title="Player deep dive", icon="🏀"),
]

st.navigation(pages).run()
