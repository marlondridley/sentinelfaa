"""
Text from outside the dashboard, made safe for the places it is shown.

Model output, stored answers, exception messages and operator notes reach pages that render Markdown, CSV and error
boxes. Markdown has links and images: an answer containing `![](https://host/x?d=...)` makes every viewer's browser
fetch that address when the page renders, and a stored answer is shown again to other people. So the text is shown as
text.

  plain(text)       the text with every Markdown control character escaped, so it renders literally
  csv_safe(frame)   a DataFrame copy whose text cells cannot be read as a spreadsheet formula
  failure(what, exc) the sentence to show when something failed; the detail goes to the log, not the page

No Streamlit import: the functions are plain and tested without it.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

# CommonMark lets any ASCII punctuation character be backslash-escaped. Escaping all of it is simpler and safer than
# choosing the ones that matter: `!` and `[` make images and links, `<` makes autolinks and HTML, `:` ends the scheme
# of a bare https:// autolink and starts an emoji shortcode, `$` starts math.
_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def plain(text: Any) -> str:
    """`text` as Markdown that renders as exactly that text: no link, image, emphasis, heading or shortcode."""
    return _PUNCTUATION.sub(r"\\\1", "" if text is None else str(text))


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: Any) -> Any:
    """A text value that a spreadsheet would run as a formula gets a leading apostrophe; anything else is unchanged."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def csv_safe(frame):
    """A copy of a DataFrame safe to export as CSV: every text cell passed through csv_cell()."""
    safe = frame.copy()
    for column in safe.columns:
        if safe[column].dtype == object:
            safe[column] = safe[column].map(csv_cell)
    return safe


def failure(what: str, exc: BaseException) -> str:
    """
    The sentence to show a signed-in user when `what` failed. The exception text can carry a host, a path, a SQL
    fragment or a model's reply, so the page gets a reference and the log gets the detail.
    """
    ref = uuid.uuid4().hex[:8]
    _log.error("dashboard_failure", extra={"what": what, "ref": ref, "error": f"{type(exc).__name__}: {exc}"})
    return f"{what}. Ask an operator to check the dashboard log (reference {ref})."
