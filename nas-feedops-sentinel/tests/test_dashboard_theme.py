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


def _stylesheet(monkeypatch) -> str:
    emitted: list[str] = []
    monkeypatch.setattr(theme.st, "markdown", lambda body, **_: emitted.append(body))
    theme.inject_theme()
    [css] = emitted
    return css


# ── The KPI strip's layout ──────────────────────────────────────────────────────
# What these pin is the mechanism, read from the stylesheet. That it does what it is for was measured in a
# browser, at 1024, 1280, 1440 and 1920 px on every page: within a strip, every tile's number at one height,
# every badge beside its label or every badge above it, no badge over a label, and no six-tile strip with a
# word running into the next tile. A change here needs that look again; there is no browser in the suite.

def test_the_drill_badge_sits_beside_the_label_never_over_it(monkeypatch):
    """
    The badge was position:absolute over the tile's top right corner, and the label kept 64px clear for it:
    enough for "OPEN →", not for "BREAKDOWN →" or "BY SERVICE →", which printed over the label on a
    laptop-width tile. The badge is now a cell of the tile's own grid: its own width, in the label's row.
    """
    css = _stylesheet(monkeypatch)

    tile = _rule(css, ".kpi:has(> .kpi-value)")
    assert "display: grid" in tile and "grid-template-columns: minmax(0, 1fr) auto" in tile
    badge, label = _rule(css, ".kpi:has(> .kpi-value) > .kpi-drill"), _rule(css, ".kpi:has(> .kpi-value) > .kpi-label")
    assert "grid-row: 2" in badge and "grid-column: 2" in badge and "white-space: nowrap" in badge
    assert "grid-row: 2" in label and "grid-column: 1" in label, "the label wraps in what the badge leaves"
    assert "grid-column: 1 / -1" in _rule(css, ".kpi:has(> .kpi-value) > .kpi-label:first-child"), \
        "a tile with no badge gives its label the whole row"
    assert not re.search(r"\.kpi-drill\s*\{[^}]*position:\s*absolute", css), "out of the row, it prints over the label"
    assert not re.search(r"\.kpi-label\s*\{[^}]*padding-right", css), "a fixed reservation fits one badge text only"

    html = theme.kpi("Schema conformance", "98.2", unit="%", href=theme.drill_href("health"), drill="BREAKDOWN →")
    assert html.index("kpi-drill") < html.index("kpi-label"), "the badge is the tile's first child: a label that is first has none"
    assert '<div class="kpi-label">Schema conformance</div><div class="kpi-value"' in html, \
        "the page tests read a tile as its label, then its value"


def test_the_numbers_of_a_strip_sit_on_one_line(monkeypatch):
    """
    Each tile laid itself out alone, so a one-line label put its number 20px above a two-line label's, and a
    tile whose badge had wrapped above its label sat lower still. The tiles now share the strip's rows.
    """
    css = _stylesheet(monkeypatch)

    strip, tile = _rule(css, ".kpis"), _rule(css, ".kpi:has(> .kpi-value)")
    assert "display: grid" in strip and "row-gap: 0" in strip, "a gap between the shared rows would open up inside every tile"
    assert "grid-template-rows: subgrid" in tile and "grid-row: span 4" in tile, "badge, label, number, footnote"
    assert "grid-row: 3" in _rule(css, ".kpi > .kpi-value") and "grid-row: 4" in _rule(css, ".kpi > .kpi-foot")
    assert "margin-bottom: 10px" in _rule(css, ".kpi"), "the space between two rows of tiles is the tile's own"
    assert "container-type" not in tile and "container-type" not in _rule(css, ".kpi"), \
        "a tile that is a size container cannot borrow the strip's rows"


def test_every_tile_of_a_narrow_strip_puts_its_badge_above_its_label(monkeypatch):
    """
    Left to each tile, a strip mixed the two layouts: the tiles with a long word or a long badge stacked and
    their neighbours did not. The strip measures itself and decides for all of them.
    """
    css = _stylesheet(monkeypatch)

    assert "container-type: inline-size" in _rule(css, ".kpis")
    for columns in range(1, 9):
        width = columns * (theme.KPI_BESIDE_MIN_PX + 31) + (columns - 1) * 10
        assert theme.kpi_stack_width(columns) == width, "tiles wider than KPI_BESIDE_MIN_PX, inside padding and border"
        [rule] = re.findall(r"@container \(max-width: " + str(width) + r"px\) \{ (\.kpis-" + str(columns) + r" .*) \}", css)
        assert f".kpis-{columns} > .kpi > .kpi-drill {{ grid-row: 1; grid-column: 1 / -1; justify-self: end; }}" in rule
        assert f".kpis-{columns} > .kpi > .kpi-label {{ grid-column: 1 / -1; }}" in rule
    # After the rules they override: the same specificity, so the later one wins.
    assert css.index(".kpi:has(> .kpi-value) > .kpi-drill") < css.index("@container (max-width:")
    assert theme.KPI_BESIDE_MIN_PX >= 197, "CONFORMANCE beside BREAKDOWN → needs 197px; narrower and that tile alone would not fit"


def test_a_small_window_folds_six_tiles_into_two_rows_of_three(monkeypatch):
    """Below about 1100px six tiles across are narrower than one word of their labels, and the words ran into the next tile."""
    css = _stylesheet(monkeypatch)
    emitted: list[str] = []
    monkeypatch.setattr(theme.st, "markdown", lambda body, **_: emitted.append(body))

    theme.kpi_row([theme.kpi("A", "1")] * 6, columns=6)
    theme.kpi_row([theme.kpi("A", "1")] * 5)

    assert '<div class="kpis kpis-6" style="grid-template-columns:repeat(6, minmax(0,1fr));">' in emitted[0]
    assert '<div class="kpis kpis-5" ' in emitted[1], "the strip says how many columns it has"
    [fold] = re.findall(r"@media \(max-width: (\d+)px\) \{\s*\.kpis\.kpis-6 \{ ([^}]*)\}", css)
    assert fold == ("1100", "grid-template-columns: repeat(3, minmax(0, 1fr)) !important; ")
    assert theme.KPI_FOLD_BELOW_PX == 1100
    assert css.count("@media (max-width: 1100px)") == 1, "only the six-tile strip folds; at wider windows nothing changes"


def test_a_facility_card_is_still_a_plain_block(monkeypatch):
    """A card with no number (the Dashboard's airports) has no rows to share; only a tile with a number is a grid."""
    css = _stylesheet(monkeypatch)
    assert "display: block" in _rule(css, ".kpi")
    assert "display: grid" in _rule(css, ".kpi:has(> .kpi-value)")
    assert not re.search(r"(?m)^\s*\.kpi > \* \{", css), "the layout rules apply to tiles with a number only"


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

