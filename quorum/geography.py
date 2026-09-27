"""Where a company's head office is, and whether that is somewhere you sell.

Two things live here: the region → country lists a focus profile selects from,
and the comparison the ICP test makes.

**Whole values, not substrings.** The test this replaces matched a country list
against a joined "city, state, country" display string, which is why that list
carried `" us"` with a leading space — without it, `"us"` matched inside
`"Australia"`, and every Australian company read as North American. Comparing
whole country values removes that class of bug rather than patching the next
instance of it: `"Austria"` is not `"Australia"` is not `"AU"`.

**Levels.** A selection is `{"level": ..., "value": ...}` at one of two levels,
`region` or `country`. A bare string is read as a region, because that is what
existing profiles hold. State and city levels are deliberately absent: matching
them needs name normalisation this does not do — `CA` is California or Canada
depending on the column, and Washington is a state and a city — and that is a
matching rule to design, not a list to extend.

**Unknown is refused, not ignored.** A profile naming a region this module does
not know used to apply no geography filter at all and say nothing, so every
company passed. Configuration is validated at `quorum init` and again before a
run starts. CRM *data* is never refused: a country an org holds that appears in
no region is simply outside the selection, which is an answer, not an error.
"""

from __future__ import annotations

from typing import Any, Iterable

# Regions as country lists, with the aliases a CRM actually contains. Written
# lowercase; comparison lowercases and strips both sides.
#
# Not a geopolitical statement — a sales-territory convention, which is what a
# focus profile means by it. A customer who divides the world differently
# selects countries instead.
REGIONS: dict[str, dict[str, Any]] = {
    "north america": {
        "short": "NA",
        "countries": {
            "united states": ["usa", "us", "u.s.", "u.s.a.", "united states of america"],
            "canada": ["ca"],
            "mexico": ["mx"],
        },
    },
    "emea": {
        "short": "EMEA",
        "countries": {
            # Europe
            "united kingdom": ["uk", "u.k.", "great britain", "england", "scotland",
                               "wales", "northern ireland", "britain"],
            "ireland": [], "france": [], "germany": [], "spain": [], "portugal": [],
            "italy": [], "netherlands": ["holland", "the netherlands"], "belgium": [],
            "luxembourg": [], "switzerland": [], "austria": [], "denmark": [],
            "sweden": [], "norway": [], "finland": [], "iceland": [], "poland": [],
            "czech republic": ["czechia"], "slovakia": [], "hungary": [],
            "romania": [], "bulgaria": [], "greece": [], "cyprus": [], "malta": [],
            "croatia": [], "slovenia": [], "serbia": [], "bosnia and herzegovina": [],
            "montenegro": [], "north macedonia": ["macedonia"], "albania": [],
            "estonia": [], "latvia": [], "lithuania": [], "belarus": [],
            "ukraine": [], "moldova": [], "russia": ["russian federation"],
            "turkey": ["türkiye", "turkiye"], "monaco": [], "liechtenstein": [],
            "andorra": [], "san marino": [],
            # Middle East
            "israel": [], "united arab emirates": ["uae", "u.a.e."],
            "saudi arabia": ["ksa"], "qatar": [], "kuwait": [], "bahrain": [],
            "oman": [], "jordan": [], "lebanon": [], "iraq": [], "iran": [],
            "yemen": [], "syria": [],
            # Africa
            "south africa": [], "nigeria": [], "kenya": [], "egypt": [],
            "morocco": [], "tunisia": [], "algeria": [], "libya": [], "ghana": [],
            "ethiopia": [], "tanzania": [], "uganda": [], "rwanda": [],
            "senegal": [], "ivory coast": ["côte d'ivoire", "cote d'ivoire"],
            "cameroon": [], "zambia": [], "zimbabwe": [], "botswana": [],
            "namibia": [], "mozambique": [], "angola": [], "mauritius": [],
            "sudan": [], "somalia": [],
        },
    },
    "apac": {
        "short": "APAC",
        "countries": {
            "australia": ["au"], "new zealand": ["nz"], "japan": [],
            "south korea": ["korea", "republic of korea"], "china": [
                "people's republic of china", "prc"],
            "hong kong": [], "taiwan": [], "singapore": [], "malaysia": [],
            "indonesia": [], "thailand": [], "vietnam": ["viet nam"],
            "philippines": ["the philippines"], "india": [], "pakistan": [],
            "bangladesh": [], "sri lanka": [], "nepal": [], "cambodia": [],
            "laos": [], "myanmar": ["burma"], "brunei": [], "mongolia": [],
            "papua new guinea": [], "fiji": [],
        },
    },
}

LEVELS = ("region", "country")


class GeographyError(ValueError):
    """A selection this module cannot act on. Raised for configuration only."""


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _country_index() -> dict[str, str]:
    """Every country name and alias -> its canonical country name."""
    index: dict[str, str] = {}
    for region in REGIONS.values():
        for country, aliases in region["countries"].items():
            index[country] = country
            for alias in aliases:
                index[alias] = country
    return index


COUNTRY_INDEX = _country_index()


def _region_countries(region: str) -> set[str]:
    return set(REGIONS[region]["countries"])


def parse_selections(raw: Iterable[Any]) -> list[dict]:
    """A focus profile's `hq_geographies` -> validated selections.

    Accepts `"North America"` as well as `{"level": "region", "value": ...}`:
    the live profiles that predate levels hold bare strings, and rewriting
    someone's configuration to add a key they did not type is not a migration
    worth doing.
    """
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, str):
            level, value = "region", item
        elif isinstance(item, dict):
            level, value = _norm(item.get("level")) or "region", item.get("value")
        else:
            raise GeographyError(
                f"{item!r} is not a geography selection. Give a region name, or "
                '{"level": "region"|"country", "value": "..."}.'
            )

        level = _norm(level)
        if level not in LEVELS:
            raise GeographyError(
                f"Geography level {level!r} is not one of {list(LEVELS)}. "
                "State and city levels need name normalisation the ICP test does "
                "not do, and are deliberately not accepted."
            )

        name = _norm(value)
        if not name:
            raise GeographyError("A geography selection needs a value.")

        if level == "region":
            if name not in REGIONS:
                raise GeographyError(
                    f"Unknown region {value!r}. Known regions: "
                    f"{', '.join(sorted(REGIONS))}. Name a country instead with "
                    '{"level": "country", "value": "..."} if the region is not one '
                    "of these."
                )
            out.append({"level": "region", "value": name})
        else:
            if name not in COUNTRY_INDEX:
                raise GeographyError(
                    f"Unknown country {value!r}. It has to be a country one of the "
                    f"regions lists ({', '.join(sorted(REGIONS))}) — see "
                    "quorum/geography.py. A country nobody can name is a filter "
                    "that silently matches nothing."
                )
            out.append({"level": "country", "value": COUNTRY_INDEX[name]})
    return out


def label(selections: list[dict]) -> str:
    """How the selections are named on a row that failed them. 'NA' for North
    America keeps the shorthand the coverage tab already used."""
    parts = []
    for sel in selections:
        if sel["level"] == "region":
            parts.append(REGIONS[sel["value"]]["short"])
        else:
            parts.append(sel["value"].title())
    return "/".join(parts) or "anywhere"


def prose_label(selections: list[dict]) -> str:
    """The same selections written out, for a sentence rather than a cell.

    `label` is built for a narrow column, where "NA" is a reasonable compression
    a reader decodes from context. A caption explaining what the ICP test was
    has no such context — it is read by whoever received the file, who has no
    reason to know the shorthand — and a caption that needs decoding is the
    thing that caption exists to stop.
    """
    parts = [sel["value"].title() for sel in selections]
    if not parts:
        return "anywhere"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " or " + parts[-1]


def matches(selections: list[dict], country: Any) -> bool:
    """Is this company's HQ country inside any selection?

    Whole-value: the country is resolved to a canonical name through the alias
    index and compared, never searched for inside a longer string.
    """
    canonical = COUNTRY_INDEX.get(_norm(country))
    if not canonical:
        return False
    for sel in selections:
        if sel["level"] == "country" and sel["value"] == canonical:
            return True
        if sel["level"] == "region" and canonical in _region_countries(sel["value"]):
            return True
    return False
