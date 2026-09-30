"""
The dashboard's text stays readable (dashboard/lib/theme.py and every page).

Section subtitles, KPI labels and footnotes had drifted down to 10-12px in a
low-contrast grey, and the browser's own title="" tooltips were delayed and
unreachable without a mouse. These tests hold the floor so it cannot drift back:
no declared font size under 13px, secondary text at WCAG AA contrast on the dark
panels, and tooltips that are focusable and carry their text in the markup.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="theme.py renders through Streamlit")

# dashboard/ is not a package on the import path: every page adds it to sys.path itself.
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from lib import theme  # noqa: E402

MIN_FONT_PX = 13


def _luminance(hex_colour: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(foreground: str, background: str) -> float:
    a, b = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def test_no_text_in_the_dashboard_is_declared_smaller_than_13px():
    offenders = []
    for path in sorted(DASHBOARD.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for size in re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", line):
                if float(size) < MIN_FONT_PX:
                    offenders.append(f"{path.relative_to(DASHBOARD)}:{number} font-size {size}px")
    assert not offenders, "text under 13px is hard to read:\n" + "\n".join(offenders)


@pytest.mark.parametrize("panel", [theme.BLACK, theme.DARK_GRAY1, theme.DARK_GRAY2])
@pytest.mark.parametrize("name", ["GRAY1", "GRAY3", "GRAY5"])
def test_secondary_text_colours_meet_wcag_aa_on_every_panel(name, panel):
    assert _contrast(getattr(theme, name), panel) >= 4.5, f"{name} on {panel}"


def test_headings_step_down_from_page_title_to_section_to_its_question():
    source = Path(theme.__file__).read_text(encoding="utf-8")       # the stylesheet is an f-string: braces are doubled
    sizes = {name: int(m.group(1)) for name in ("sec-title", "sec-q", "sec-right")
             if (m := re.search(r"\." + name + r" \{\{[^}]*?font-size: (\d+)px", source))}
    assert set(sizes) == {"sec-title", "sec-q", "sec-right"}, sizes
    assert sizes["sec-title"] > sizes["sec-q"] > sizes["sec-right"] >= MIN_FONT_PX, sizes


def test_a_hint_is_a_focusable_tooltip_with_its_text_in_the_markup_not_a_title_attribute():
    html = theme.help_hint("What this figure means.")
    assert 'class="tip"' in html and 'tabindex="0"' in html and 'data-tip="What this figure means."' in html
    assert 'aria-label="What this figure means."' in html
    assert " title=" not in html


def test_an_evidence_chip_is_a_focusable_tooltip_too():
    html = theme.event_chips(["evt-1"], {"evt-1": {"service_name": "SMES", "airport_code": "KIAD",
                                                   "event_time_utc": None}})
    assert "tip" in html and 'tabindex="0"' in html and "data-tip=" in html and " title=" not in html
    assert "SMES" in html and "KIAD" in html


def test_the_page_header_and_a_section_read_at_a_comfortable_size(monkeypatch):
    seen = []
    monkeypatch.setattr(theme.st, "markdown", lambda body, **kwargs: seen.append(body))
    theme.page_header("AI Critic & Human Review", "Which alerts are safety-significant?", detail="More.")
    theme.section("Operational readiness", "Every figure opens the page that explains it.", right="advisory only")
    header = seen[0]
    assert "font-size:30px" in header and "font-size:16px" in header and "font-style:italic" not in header
    assert 'class="tip"' in header, "the page's detail is a real tooltip"
