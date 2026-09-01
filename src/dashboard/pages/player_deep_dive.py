"""Player deep dive: efficiency and production trends across the season."""

from __future__ import annotations

import streamlit as st

from src.dashboard import charts, queries

st.title("Player deep dive")

players = queries.get_players(min_games=20)
if players.empty:
    st.warning("No players found. Run the ingestion and transform layers first.")
    st.stop()

# Label carries team and position so duplicate names stay distinguishable.
players = players.assign(
    label=players["player_name"]
    + players["team"].fillna("—").radd(" · ")
    + players["position"].fillna("").radd(" · ").where(players["position"].notna(), "")
)

# The list stays alphabetical so a player is easy to find, but the page opens
# on the season's leading scorer rather than whoever sorts first.
default_index = int(players["ppg"].idxmax())

left, right = st.columns([3, 2])
with left:
    label = st.selectbox("Player", players["label"], index=default_index)
with right:
    season_type = st.selectbox(
        "Games", ["regular", "playoffs", "playin"], index=0,
        format_func={"regular": "Regular season", "playoffs": "Playoffs",
                     "playin": "Play-in"}.get,
    )

player = players[players["label"] == label].iloc[0]
player_id = int(player["player_id"])

summary = queries.get_player_summary(player_id, season_type)
log = queries.get_player_game_log(player_id, season_type)

if log.empty or summary["games"] == 0:
    st.info(f"{player['player_name']} has no {season_type} games in the warehouse.")
    st.stop()

# --- headline numbers -------------------------------------------------------
# Units live in the label: a `delta` would render an arrow implying change.
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Games", int(summary["games"]),
          help=f"{summary['mpg']:.1f} minutes per game")
c2.metric("PPG", f"{summary['ppg']:.1f}", help="Points per game")
c3.metric("RPG", f"{summary['rpg']:.1f}", help="Rebounds per game")
c4.metric("APG", f"{summary['apg']:.1f}", help="Assists per game")
# TS% is the metric a box score can't show you — it prices threes and free
# throws correctly, so it belongs beside the counting stats.
c5.metric("True shooting", f"{summary['ts_pct']:.1%}",
          help="Points per shooting possession: PTS / (2 x (FGA + 0.44 x FTA))")

st.divider()

# --- production and efficiency ----------------------------------------------
# Points and TS% have different units, so they get separate charts — never a
# second y-axis.
st.altair_chart(
    charts.trend_chart(
        log, rolling_field="roll_pts", raw_field="pts",
        title=f"Rolling 10-game scoring — {player['player_name']}",
        y_title="Points",
    ),
    use_container_width=True,
)

st.altair_chart(
    charts.trend_chart(
        log, rolling_field="roll_ts", raw_field="true_shooting_pct",
        title="Rolling 10-game true shooting %",
        y_title="True shooting %",
        color=charts.AQUA, percent=True,
    ),
    use_container_width=True,
)
st.caption(
    "TS% = points ÷ (2 × (FGA + 0.44 × FTA)). Games with no shot attempts are "
    "excluded rather than counted as 0%."
)

st.altair_chart(
    charts.dual_series_chart(
        log,
        fields={"roll_reb": "Rebounds", "roll_ast": "Assists"},
        title="Rolling 10-game rebounds and assists",
        y_title="Per game",
        colors=(charts.ORANGE, charts.BLUE),
    ),
    use_container_width=True,
)

# --- home / away ------------------------------------------------------------
st.divider()
st.subheader("Home and away")

splits = queries.get_player_home_away(player_id, season_type)
s1, s2 = st.columns(2)
with s1:
    st.altair_chart(
        charts.split_bar_chart(splits, "venue", "ppg", "Points per game", "PPG"),
        use_container_width=True,
    )
with s2:
    ts = splits.assign(ts_pct_display=splits["ts_pct"] * 100)
    st.altair_chart(
        charts.split_bar_chart(ts, "venue", "ts_pct_display",
                               "True shooting %", "TS %"),
        use_container_width=True,
    )

# --- game log ---------------------------------------------------------------
with st.expander("Game log"):
    table = log.assign(
        Venue=log["is_home"].map({True: "Home", False: "Away"}),
        FG=log["fgm"].astype(str) + "-" + log["fga"].astype(str),
        TP=log["fg3m"].astype(str) + "-" + log["fg3a"].astype(str),
    )[["game_date", "opponent", "Venue", "min", "pts", "reb", "ast",
       "FG", "TP", "true_shooting_pct", "plus_minus"]]
    table.columns = ["Date", "Opponent", "Venue", "Min", "PTS", "REB", "AST",
                     "FG", "3P", "TS%", "+/-"]
    st.dataframe(table, use_container_width=True, hide_index=True,
                 column_config={"TS%": st.column_config.NumberColumn(format="%.1f%%")})
