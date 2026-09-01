"""Team trends: rolling form, efficiency, and home/away splits."""

from __future__ import annotations

import streamlit as st

from src.dashboard import charts, queries

st.title("Team trends")

teams = queries.get_teams()
if teams.empty:
    st.warning("No teams found. Run the ingestion and transform layers first.")
    st.stop()

# Filters in one row above the charts.
left, right = st.columns([3, 2])
with left:
    team_name = st.selectbox("Team", teams["full_name"], index=0)
with right:
    season_type = st.selectbox(
        "Games", ["regular", "playoffs", "playin"], index=0,
        format_func={"regular": "Regular season", "playoffs": "Playoffs",
                     "playin": "Play-in"}.get,
    )

team = teams[teams["full_name"] == team_name].iloc[0]
summary = queries.get_team_summary(int(team["team_id"]), season_type)
log = queries.get_team_game_log(int(team["team_id"]), season_type)

if log.empty:
    st.info(f"{team_name} has no {season_type} games in the warehouse.")
    st.stop()

st.caption(f"{team['conference']}ern Conference · {team['division']} Division")

# --- headline numbers -------------------------------------------------------
# Ratings are the story here, so they lead. Net rating carries a sign, so it
# gets an explicit + and a coloured delta rather than a bare number.
# Units live in the label: st.metric renders any `delta` with an up/down
# arrow, which would imply a change these numbers don't describe.
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Record", f"{int(summary['wins'])}–{int(summary['losses'])}",
          help=f"{100 * summary['wins'] / summary['games']:.1f}% win rate")
c2.metric("Net rating", f"{summary['net_rating']:+.1f}",
          help="Points outscored per 100 possessions")
c3.metric("Off. rating", f"{summary['off_rating']:.1f}",
          help="Points scored per 100 possessions")
c4.metric("Def. rating", f"{summary['def_rating']:.1f}",
          help="Points allowed per 100 possessions — lower is better")
c5.metric("Pace", f"{summary['pace']:.1f}",
          help="Possessions per 48 minutes")
st.caption("Ratings are per 100 possessions, so they compare teams that play at different speeds.")

st.divider()

# --- rolling form -----------------------------------------------------------
st.altair_chart(
    charts.trend_chart(
        log, rolling_field="roll_net", raw_field="net_rating",
        title=f"Rolling 10-game net rating — {team['abbreviation']}",
        y_title="Net rating (pts / 100 poss.)",
        zero_line=True,
    ),
    use_container_width=True,
)
st.caption(
    "Faint dots are individual games; the line is the 10-game average. "
    "The dashed rule is zero — above it the team outscored opponents per possession."
)

# Offensive and defensive rating share a unit, so they belong on one scale.
# Note a *lower* defensive rating is better: the lines converging is the team
# getting worse, not better.
st.altair_chart(
    charts.dual_series_chart(
        log,
        fields={"roll_off": "Offensive rating", "roll_def": "Defensive rating"},
        title="Rolling 10-game offensive vs defensive rating",
        y_title="Points per 100 possessions",
    ),
    use_container_width=True,
)
st.caption("Lower is better for defensive rating — the gap between the lines is net rating.")

# --- home / away ------------------------------------------------------------
st.divider()
st.subheader("Home and away")

splits = queries.get_team_home_away(int(team["team_id"]), season_type)
s1, s2 = st.columns(2)
with s1:
    st.altair_chart(
        charts.split_bar_chart(splits, "venue", "win_pct",
                               "Win percentage", "Win %"),
        use_container_width=True,
    )
with s2:
    st.altair_chart(
        charts.split_bar_chart(splits, "venue", "net_rating",
                               "Net rating", "Net rating", value_format="+.1f"),
        use_container_width=True,
    )

# --- game log ---------------------------------------------------------------
with st.expander("Game log"):
    table = log.assign(
        Result=log["won"].map({True: "W", False: "L"}),
        Venue=log["is_home"].map({True: "Home", False: "Away"}),
        Score=log["pts"].astype(str) + "–" + log["opp_pts"].astype(str),
    )[["game_date", "opponent", "Venue", "Result", "Score",
       "off_rating", "def_rating", "net_rating", "pace"]]
    table.columns = ["Date", "Opponent", "Venue", "Result", "Score",
                     "ORtg", "DRtg", "NetRtg", "Pace"]
    st.dataframe(table, use_container_width=True, hide_index=True)
