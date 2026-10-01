"""
Tests for the dashboard's design system (dashboard/lib/theme.py).

Two things here are worth testing and one is not. Not worth testing: which
shade of grey a panel is. Worth testing:

  1. The drill-down links. Every KPI tile on the Overview page is a link
     into a pre-filtered page, and a link built with the wrong slug or a
     dropped parameter fails silently -- the operator lands on an
     unfiltered page and has no way to tell that the filter was lost.
     drill_href() is the single place those URLs are built, so pinning its
     output pins every drill-down in the app.
  2. The escaping. Alert descriptions, Watch Officer headlines and airport
     codes all reach the browser through unsafe_allow_html=True. They come
     from the database and from LLM output. If _esc() ever stops escaping,
     nothing visible breaks until something is rendered that shouldn't be.

The sparkline edge cases are here because a flat series is the normal
state of a healthy system, and dividing by a zero range would blank the
tile exactly when everything is fine.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="theme.py renders through Streamlit")

# dashboard/ is not a package on the import path -- every page adds it to
# sys.path itself when Streamlit runs it. Tests have to do the same.
_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
if str(_DASHBOARD) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD))

from lib import theme  # noqa: E402

XSS = '<img src=x onerror="alert(1)">'


# ── Drill-down links ──────────────────────────────────────────────────────────

def test_alert_drilldown_carries_the_severity_filter():
    assert theme.drill_href("alerts", severity="CRITICAL") == "Alert_Log?severity=CRITICAL"


def test_drilldown_without_filters_is_a_bare_page_link():
    assert theme.drill_href("alerts") == "Alert_Log"


def test_none_and_empty_filters_are_dropped_rather_than_sent_blank():
    """Callers pass optional filters unconditionally; an `airport=` with no
    value would filter the target page down to nothing."""
    assert theme.drill_href("alerts", airport=None, service="") == "Alert_Log"


def test_multiple_filters_are_joined_as_a_query_string():
    assert theme.drill_href("health", focus="latency", airport="KIAD") == (
        "Feed_Health?focus=latency&airport=KIAD"
    )


def test_every_page_referenced_by_a_drilldown_has_a_slug():
    assert {"alerts", "health", "airspace", "critic", "ingest"} <= set(theme.PAGES)


@pytest.mark.parametrize(
    "key,expected",
    [("alerts", "Alert_Log"), ("health", "Feed_Health"),
     ("airspace", "Live_Airspace"), ("critic", "AI_Critic_and_Review"),
     ("evaluation", "Agent_Evaluation")],
)
def test_slugs_match_the_page_filenames_streamlit_serves(key, expected):
    """Streamlit derives the URL from the filename under dashboard/pages/
    (4_Alert_Log.py -> /Alert_Log). A rename there silently breaks every
    link built here, so the mapping is asserted against the files."""
    assert theme.PAGES[key] == expected
    matches = list((_DASHBOARD / "pages").glob("*_%s.py" % expected))
    assert matches, "no page file backs the %r slug" % expected


# ── Escaping ──────────────────────────────────────────────────────────────────

def test_kpi_label_is_escaped():
    assert "<img" not in theme.kpi(XSS, "1")


def test_kpi_value_is_escaped():
    assert "<img" not in theme.kpi("Open alerts", XSS)


def test_kpi_footnote_is_escaped():
    assert "<img" not in theme.kpi("Open alerts", "1", foot=XSS)


def test_status_chip_is_escaped():
    assert "<img" not in theme.status_chip(XSS)


def test_escaping_turns_markup_into_visible_text_rather_than_dropping_it():
    """The payload must survive as readable text -- silently discarding part of
    an alert description would be its own kind of wrong."""
    rendered = theme.kpi("x", XSS)
    assert "&lt;img" in rendered


# ── Sparklines ────────────────────────────────────────────────────────────────

def test_flat_series_does_not_divide_by_zero():
    """A steady system produces a constant series; that is the case that must
    not blank the tile."""
    assert theme.sparkline([5, 5, 5, 5]) != ""


def test_single_point_and_empty_series_draw_nothing():
    assert theme.sparkline([7]) == ""
    assert theme.sparkline([]) == ""


def test_none_values_are_skipped_rather_than_plotted_as_zero():
    assert theme.sparkline([1, None, 3]) != ""


def test_sparkline_coordinates_are_finite_numbers():
    svg = theme.sparkline([0, 10, 5, 8])
    points = re.search(r'points="([^"]+)"', svg).group(1)
    for pair in points.split():
        x, y = pair.split(",")
        assert float(x) == float(x)  # not NaN
        assert float(y) == float(y)


# ── Status vocabulary ─────────────────────────────────────────────────────────

def test_no_data_is_neutral_and_never_reads_as_healthy():
    """"Nothing has ever arrived" and "arrived and was fine" are different
    claims; rendering the first in green would assert the second."""
    assert theme.STATUS_TONE["NO DATA"] == "neutral"


def test_unavailable_and_disconnected_are_bad():
    assert theme.STATUS_TONE["UNAVAILABLE"] == "bad"
    assert theme.STATUS_TONE["DISCONNECTED"] == "bad"


def test_unknown_status_falls_back_to_neutral_instead_of_raising():
    assert "<span" in theme.status_chip("something-nobody-defined")


def test_status_chip_normalises_case():
    assert ">OK<" in theme.status_chip("ok")


# ── KPI tiles ─────────────────────────────────────────────────────────────────

def test_linked_tile_is_an_anchor_and_shows_a_drill_affordance():
    tile = theme.kpi("Open alerts", "205", href=theme.drill_href("alerts"))
    assert tile.startswith('<a class="kpi"')
    assert "kpi-drill" in tile


def test_unlinked_tile_is_a_div_with_no_drill_affordance():
    """A tile that offers no drill-down must not look like one."""
    tile = theme.kpi("Messages ingested", "319")
    assert tile.startswith('<div class="kpi"')
    assert "kpi-drill" not in tile


def _rule(css: str, selector: str) -> str:
    """The declarations of the stylesheet's one rule with exactly this selector."""
    [declarations] = re.findall(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return declarations


def test_the_drill_badge_sits_beside_the_label_never_over_it(monkeypatch):
    """
    The badge was position:absolute over the tile's top right corner, and the label kept 64px clear for it:
    enough for "OPEN →", not for "BREAKDOWN →" or "BY SERVICE →", which printed over the label on a
    laptop-width tile. The badge is now in the label's row at its own width, whatever it says.
    """
    emitted: list[str] = []
    monkeypatch.setattr(theme.st, "markdown", lambda body, **_: emitted.append(body))
    theme.inject_theme()
    [css] = emitted

    tile, label, badge = _rule(css, ".kpi"), _rule(css, ".kpi > .kpi-label"), _rule(css, ".kpi > .kpi-drill")
    assert "display: flex" in tile and "row-reverse wrap" in tile
    assert "min-width: min-content" in label, "a tile too narrow for both wraps the badge above; it never splits a word"
    assert "flex: 0 0 auto" in badge and "white-space: nowrap" in badge
    assert not re.search(r"\.kpi-drill\s*\{[^}]*position:\s*absolute", css), "out of the row, it prints over the label"
    assert not re.search(r"\.kpi-label\s*\{[^}]*padding-right", css), "a fixed reservation fits one badge text only"

    html = theme.kpi("Schema conformance", "98.2", unit="%", href=theme.drill_href("health"), drill="BREAKDOWN →")
    assert html.index("kpi-drill") < html.index("kpi-label"), "row-reverse: the first of the two is the one on the right"
    assert '<div class="kpi-label">Schema conformance</div><div class="kpi-value"' in html, \
        "the page tests read a tile as its label, then its value"


def test_map_tooltips_escape_what_they_interpolate():
    """
    The deck.gl tooltip is HTML assigned to innerHTML, and aircraft_id is an
    unconstrained string from the feed or an API client: unescaped, a crafted id
    runs script in an operator's signed-in session when they hover the aircraft.
    """
    import ast
    from pathlib import Path

    page = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "1_Live_Airspace.py"
    tips = [value for node in ast.walk(ast.parse(page.read_text(encoding="utf-8")))
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "tip"]
    assert len(tips) == 2, "the airport and the aircraft tooltips"
    for tip in tips:
        assert isinstance(tip, ast.Call) and ast.unparse(tip.func) == "theme._esc", ast.unparse(tip)[:80]

