"""How two names are compared, in one place.

Used by the enrichment pass (does a provider's record agree with the CRM
contact?) and by the duplicate checks (is this the same person twice?), so the
two cannot drift into different ideas of "the same name".
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


def name_tokens(name: Optional[str]) -> list[str]:
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    # An apostrophe joins ("O'Neil" is "oneil"); any other punctuation separates.
    plain = re.sub(r"['’]", "", plain.lower())
    return re.sub(r"[^a-z\s]", " ", plain).split()


def name_key(name: Optional[str]) -> tuple[str, str]:
    """First and last name, normalised; () when there is no name to compare.
    "Dana M. Reyes" and "Dana Reyes" have the same key."""
    t = name_tokens(name)
    return (t[0], t[-1]) if t else ()
