"""Shared HTML joins for the Production Console panels.

Django 5 escapes the *separator* passed to ``format_html_join`` too, so a
plain ``"<br>"`` / ``" &nbsp; "`` renders as literal text (DRF-2079). The
separators below are constant markup we own — the only strings marked safe;
every joined part still goes through ``format_html`` escaping.
"""

from django.utils.html import format_html_join
from django.utils.safestring import mark_safe

LINE_BREAK = mark_safe("<br>")
BUTTON_GAP = mark_safe(" &nbsp; ")


def lines_html(lines):
    """Join already-escaped/format_html parts (or plain text) with <br>."""
    return format_html_join(LINE_BREAK, "{}", ((line,) for line in lines))


def buttons_html(links):
    """(url, label) pairs as admin buttons separated by a non-breaking gap."""
    return format_html_join(BUTTON_GAP, '<a class="button" href="{}">{}</a>', links)
