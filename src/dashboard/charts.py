"""Altair chart builders and the shared visual language.

Colours come from a validated categorical palette. Two rules this module exists
to enforce:

  * Never two y-scales on one chart. Where two measures share a unit (offensive
    and defensive rating are both points per 100 possessions) they go on one
    chart as two series; where they don't, they get separate charts.
  * A single series needs no legend — the title names it. Two or more always
    get one, so identity is never carried by colour alone.

The app commits to a light surface (see .streamlit/config.toml), so these steps
are validated against that surface rather than flipped for dark.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

# --- palette -----------------------------------------------------------------
# Categorical slots, in fixed order — never cycled, never reassigned by rank.
BLUE   = "#2a78d6"   # slot 1
ORANGE = "#eb6834"   # slot 2
AQUA   = "#1baf7a"   # slot 3

# Diverging pair for signed quantities (net rating above/below zero).
POS, NEG = "#2a78d6", "#e34948"

# Chrome and ink. Text never wears a series colour.
SURFACE   = "#fcfcfb"
INK       = "#0b0b0b"
INK_MUTED = "#898781"
GRID      = "#e1e0d9"
BASELINE  = "#c3c2b7"

CHART_HEIGHT = 260


def _style(chart: alt.Chart) -> alt.Chart:
    """Recessive chrome: hairline grid, muted axis ink, no view border."""
    return (
        chart
        .configure_view(strokeWidth=0, fill=SURFACE)
        .configure_axis(
            grid=True, gridColor=GRID, gridWidth=1,
            domainColor=BASELINE, tickColor=BASELINE,
            labelColor=INK_MUTED, titleColor=INK_MUTED,
            labelFontSize=11, titleFontSize=11, titleFontWeight="normal",
        )
        .configure_legend(
            labelColor=INK, titleColor=INK_MUTED,
            labelFontSize=11, titleFontSize=11, orient="top",
            direction="horizontal", offset=4, symbolStrokeWidth=3,
        )
        .configure_title(color=INK, fontSize=13, fontWeight=600, anchor="start")
    )


def trend_chart(
    df: pd.DataFrame,
    rolling_field: str,
    raw_field: str,
    title: str,
    y_title: str,
    color: str = BLUE,
    zero_line: bool = False,
    y_format: str = ".1f",
    percent: bool = False,
) -> alt.Chart:
    """Individual games as faint dots, the rolling average as the bold line.

    Showing both is the point: the dots are the noise, the line is the signal.
    A rolling average alone hides how volatile the underlying games were.
    """
    if df.empty:
        return _style(alt.Chart(pd.DataFrame({"x": []})).mark_point())

    axis_format = "%" if percent else y_format
    tooltip_format = ".1%" if percent else y_format

    x = alt.X("game_date:T", title=None,
              axis=alt.Axis(format="%b", tickCount="month"))

    # Zero is meaningful for net rating, so the scale must include it and the
    # baseline must be visible. Elsewhere let the scale fit the data.
    y_scale = alt.Scale(zero=zero_line, nice=True)
    y = alt.Y(f"{rolling_field}:Q", title=y_title,
              scale=y_scale, axis=alt.Axis(format=axis_format))

    # Hover on the nearest game, with a hit target the width of the plot band
    # rather than the mark itself.
    hover = alt.selection_point(
        nearest=True, on="pointerover", fields=["game_date"], empty=False,
    )

    base = alt.Chart(df)

    raw_dots = base.mark_circle(size=22, opacity=0.28, color=color).encode(
        x=x, y=alt.Y(f"{raw_field}:Q", title=y_title, scale=y_scale),
    )

    line = base.mark_line(strokeWidth=2, color=color, interpolate="monotone").encode(x=x, y=y)

    # Invisible wide bars carry the hover, so the pointer never has to find a dot.
    hit = base.mark_bar(opacity=0).encode(
        x=x,
        tooltip=[
            alt.Tooltip("game_date:T", title="Date", format="%b %d"),
            alt.Tooltip("opponent:N", title="Opponent"),
            alt.Tooltip(f"{raw_field}:Q", title="Game", format=tooltip_format),
            alt.Tooltip(f"{rolling_field}:Q", title="10-game avg", format=tooltip_format),
        ],
    ).add_params(hover)

    marker = base.mark_circle(size=80, color=color).encode(
        x=x, y=y, opacity=alt.condition(hover, alt.value(1), alt.value(0)),
    )

    layers = [raw_dots, line, hit, marker]

    if zero_line:
        rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
            color=BASELINE, strokeWidth=1, strokeDash=[4, 3],
        ).encode(y="y:Q")
        layers.insert(0, rule)

    return _style(
        alt.layer(*layers).properties(height=CHART_HEIGHT, title=title)
    )


def dual_series_chart(
    df: pd.DataFrame,
    fields: dict[str, str],
    title: str,
    y_title: str,
    colors: tuple[str, str] = (BLUE, ORANGE),
) -> alt.Chart:
    """Two series that share a unit, on ONE y-scale.

    Valid here because offensive and defensive rating are both "points per 100
    possessions". Two measures with different units would need two charts.
    """
    if df.empty:
        return _style(alt.Chart(pd.DataFrame({"x": []})).mark_point())

    long = df.melt(
        id_vars=["game_date"], value_vars=list(fields),
        var_name="series", value_name="value",
    ).replace({"series": fields})

    order = list(fields.values())

    chart = alt.Chart(long).mark_line(strokeWidth=2, interpolate="monotone").encode(
        x=alt.X("game_date:T", title=None, axis=alt.Axis(format="%b", tickCount="month")),
        y=alt.Y("value:Q", title=y_title, scale=alt.Scale(zero=False, nice=True)),
        color=alt.Color("series:N", title=None,
                        scale=alt.Scale(domain=order, range=list(colors))),
        tooltip=[
            alt.Tooltip("game_date:T", title="Date", format="%b %d"),
            alt.Tooltip("series:N", title="Metric"),
            alt.Tooltip("value:Q", title="10-game avg", format=".1f"),
        ],
    ).properties(height=CHART_HEIGHT, title=title)

    return _style(chart)


def split_bar_chart(
    df: pd.DataFrame, category: str, value: str, title: str, y_title: str,
    value_format: str = ".1f",
) -> alt.Chart:
    """Small categorical comparison (home vs away), direct-labelled.

    Two bars need no legend — the axis names them, and each carries its value.
    """
    if df.empty:
        return _style(alt.Chart(pd.DataFrame({"x": []})).mark_bar())

    base = alt.Chart(df).encode(
        x=alt.X(f"{category}:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{value}:Q", title=y_title, scale=alt.Scale(zero=True)),
    )

    bars = base.mark_bar(size=54, cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                         color=BLUE)
    labels = base.mark_text(dy=-8, color=INK, fontSize=12).encode(
        text=alt.Text(f"{value}:Q", format=value_format),
    )

    return _style(
        alt.layer(bars, labels).properties(height=200, title=title)
    )
