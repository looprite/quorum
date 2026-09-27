"""The CRM field map — resolved from the org, never assumed.

Standard CRM fields are portable; custom fields are not, and custom fields are
where the signal lives. A hardcoded custom name is not a cosmetic wart: a field
that does not exist raises INVALID_FIELD and kills the whole query rather than
blanking a column, so it is a run that dies at the next customer. It is also
per-customer differentiation living in code, which forces the fork this
repository bans.

So the repository holds patterns, not names. At configure time this module
describes the object, matches its fields against those patterns, counts how many
rows actually have each candidate populated, and writes the survivors — in
count order — into the account's field map. Every query is then built from the
map.

**Ordered candidates, not a single winner.** The first populated value wins at
read time. On one real org the reason is concrete: some of the companies met in
a week have an empty managed-package country field and a populated
`BillingCountry`. A map holding only the better-populated field would blank
their HQ and drop them out of the ICP set. Ordering decides which field is asked first; the chain is what
stops a per-row gap becoming a wrong answer.

**Why counting is not enough on its own.** On the same org the best-populated
field matching /linkedin/ on Contact is the *company's* page, not the person's,
and it beats the field the pipeline wants. Patterns therefore carry exclusions
as well as inclusions, and
every rejection is recorded with the rule that made it, so the choice can be
argued with rather than taken on trust.

Unresolved optional fields degrade their column to "not available in this CRM".
Unresolved *required* fields — the ones a decision is made on — stop the
resolution rather than producing a run whose ICP test quietly passes everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

NOT_AVAILABLE = "not available in this CRM"

# The CRM was never asked, so there is no answer to report — as distinct from
# NOT_AVAILABLE, which means a CRM *was* asked and holds no such field. Both
# differ again from a plain False, which is a CRM answering "no". Collapsing any
# of the three into another has been the same defect three times over.
NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class FieldSpec:
    """One logical field the pipeline reads, and how to find it in any org."""

    logical: str
    # What the artifact does with it — the read path that justifies resolving it.
    serves: str
    # Matched against "<api name> <label>", case-insensitively.
    include: str
    exclude: str
    # SOQL types that can hold the value. A headcount in a picklist is a band,
    # not a number; a LinkedIn profile in a textarea is a note, not a URL.
    types: tuple[str, ...]
    # Required fields drive a decision (the ICP test). Optional ones only fill a
    # column, and their absence is stated in that column.
    required: bool = False


TEXT = ("string", "url", "picklist", "textarea", "email", "phone")
NUMERIC = ("int", "double", "currency", "percent")

# The five logical fields the pipeline needs from a CRM. Nothing is specced "in
# case" — each names the column it serves.
SPECS: dict[str, tuple[FieldSpec, ...]] = {
    "Account": (
        FieldSpec(
            logical="employee_count",
            serves="tab 2 'Employees', and the ICP employee band",
            include=r"employee|headcount|staff",
            # A range, a band or a percentage of headcount is not a headcount.
            # Seen in the wild: a double field labelled '20% of Employees'.
            exclude=r"range|band|bracket|percent|%|[\s_]of[\s_]",
            types=NUMERIC,
            required=True,
        ),
        FieldSpec(
            logical="hq_country",
            serves="tab 2 'HQ', and the ICP geography test",
            # Shipping is a delivery address, not a head office. A *Code field
            # holds 'US' where its sibling holds 'United States' — a different
            # value space, and mixing the two is how a country list stops
            # matching.
            include=r"country",
            exclude=r"code|shipping|other\b|mailing",
            types=TEXT,
            required=True,
        ),
        FieldSpec(
            logical="hq_city",
            serves="tab 2 'HQ' (display only)",
            include=r"city",
            exclude=r"code|shipping|other\b|mailing",
            types=TEXT,
        ),
        FieldSpec(
            logical="hq_state",
            serves="tab 2 'HQ' (display only)",
            # Word-bounded: 'Value Statements' and 'Status Info' both contain
            # the letters of 'state' and neither is one.
            include=r"\bstate\b|\bprovince\b",
            exclude=r"code|shipping|other\b|mailing|status|statement",
            types=TEXT,
        ),
    ),
    "Contact": (
        FieldSpec(
            logical="linkedin_url",
            serves="tab 1 'LinkedIn?' and tab 3 'LinkedIn'",
            # The person's profile URL. Not the company's page, not a scraped
            # bio, not a location string, not a boolean 'uses LinkedIn'.
            include=r"linked_?in",
            exclude=r"company|location|bio|overview|using|status|score",
            types=("url", "string"),
        ),
    ),
}

class FieldMapError(RuntimeError):
    """A required logical field resolved to nothing."""


def _text(field: dict) -> str:
    return f"{field.get('name') or ''} {field.get('label') or ''}"


def candidates(fields: Iterable[dict], spec: FieldSpec) -> tuple[list[dict], list[dict]]:
    """Split an object's fields into (candidates, rejected-with-a-reason).

    Every rejection carries the rule that made it. A field map nobody can argue
    with is a field map nobody can check.
    """
    include = re.compile(spec.include, re.I)
    exclude = re.compile(spec.exclude, re.I)

    kept: list[dict] = []
    rejected: list[dict] = []
    for f in fields:
        text = _text(f)
        if not include.search(text):
            continue  # not a candidate at all — not worth recording
        if exclude.search(text):
            rejected.append({"field": f.get("name"), "why": "excluded by pattern"})
        elif f.get("type") not in spec.types:
            rejected.append(
                {"field": f.get("name"), "why": f"type {f.get('type')} cannot hold this"}
            )
        elif not f.get("filterable", True):
            # Unfilterable fields cannot be counted, so they cannot be ranked by
            # what has data — and ranking by data is the whole method.
            rejected.append({"field": f.get("name"), "why": "not filterable"})
        else:
            kept.append(f)
    return kept, rejected


def resolve(sf, log=print) -> tuple[dict, dict]:
    """Describe, match, count, order. -> (field_map, provenance).

    One describe and one row count per object, plus one count per candidate.
    Configure-time cost, paid once, not per run.
    """
    field_map: dict[str, dict[str, list[str]]] = {}
    provenance: dict[str, Any] = {}

    for sobject, specs in SPECS.items():
        fields = sf.describe(sobject).get("fields") or []
        total = sf.count(sobject)
        log(f"[*] {sobject}: {len(fields)} fields, {total} rows")
        field_map[sobject] = {}
        provenance[sobject] = {"total_rows": total, "fields": {}}

        for spec in specs:
            kept, rejected = candidates(fields, spec)
            counted = []
            for f in kept:
                populated = sf.count(sobject, f"{f['name']} != null")
                counted.append(
                    {
                        "field": f["name"],
                        "type": f.get("type"),
                        "populated": populated,
                        "pct": round(100 * populated / total, 1) if total else 0.0,
                    }
                )
            # Best-populated first; the name breaks ties so the map is stable
            # across re-resolutions of an unchanged org.
            counted.sort(key=lambda c: (-c["populated"], c["field"]))

            field_map[sobject][spec.logical] = [c["field"] for c in counted]
            provenance[sobject]["fields"][spec.logical] = {
                "serves": spec.serves,
                "required": spec.required,
                "candidates": counted,
                "rejected": rejected,
            }
            shown = ", ".join(f"{c['field']} ({c['pct']}%)" for c in counted) or "—"
            log(f"[*]   {spec.logical}: {shown}")

            if spec.required and not counted:
                raise FieldMapError(
                    f"No usable {sobject} field for {spec.logical!r} "
                    f"({spec.serves}). Looked for /{spec.include}/ with a type in "
                    f"{list(spec.types)}; rejected {[r['field'] for r in rejected]}. "
                    "Without it the ICP test cannot be applied, and a run would "
                    "report every company as a fit."
                )

    return field_map, provenance


class FieldMap:
    """The resolved map, as the pipeline uses it.

    Empty is a legitimate state: a run with no Salesforce configured reads no
    CRM fields at all, and every method here answers accordingly.
    """

    def __init__(self, data: Optional[dict] = None) -> None:
        self._data = data or {}

    def __bool__(self) -> bool:
        return bool(self._data)

    def names(self, sobject: str, logical: str) -> list[str]:
        """Every API name for this logical field, best-populated first."""
        return list((self._data.get(sobject) or {}).get(logical) or [])

    def available(self, sobject: str, logical: str) -> bool:
        return bool(self.names(sobject, logical))

    def select(self, sobject: str, standard: Iterable[str]) -> str:
        """A SOQL select list: the standard fields, then everything mapped.

        De-duplicated and order-stable, because a resolved name can also be a
        standard one (`NumberOfEmployees`) and SOQL will not take it twice.
        """
        out: list[str] = []
        for name in list(standard) + [
            n for logical in (self._data.get(sobject) or {}) for n in self.names(sobject, logical)
        ]:
            if name not in out:
                out.append(name)
        return ", ".join(out)

    def value(self, record: Optional[dict], sobject: str, logical: str) -> Any:
        """The first populated candidate, as the CRM typed it. '' when none has
        a value.

        This is where the chain earns itself: the best-populated field org-wide
        is still empty on plenty of individual rows.

        The value is returned unconverted — a headcount stays an int. Stringing
        it here would push a number into the artifact as text and quietly change
        every Employees cell in the workbook.
        """
        for name in self.names(sobject, logical):
            v = (record or {}).get(name)
            if v is not None and str(v).strip() != "":
                return v.strip() if isinstance(v, str) else v
        return ""


def describe_lines(field_map: dict) -> list[str]:
    """One line per logical field: what it resolved to, in order. For a log, an
    init report, or anyone asking what this run is about to read."""
    out = []
    for sobject in sorted(field_map or {}):
        for logical in sorted(field_map[sobject]):
            names = field_map[sobject][logical] or []
            out.append(
                f"{sobject}.{logical}: " + (" → ".join(names) if names else NOT_AVAILABLE)
            )
    return out
