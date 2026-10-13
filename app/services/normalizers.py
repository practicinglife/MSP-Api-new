"""Field normalisation utilities used by all platform connectors."""

import functools
import json
import os
import re

# Legal suffixes to strip from company names (order matters — longer first)
_LEGAL_SUFFIXES: tuple[str, ...] = (
    "limited liability company",
    "limited liability partnership",
    "incorporated",
    "corporation",
    "company",
    "companies",
    "limited",
    "rentals",  # Business type: Acme Rentals → Acme
    "rental",   # Singular form
    "llc",
    "ltd",
    "inc",
    "corp",
    "lp",
    "llp",
    "co",
)

# Common stop words stripped from company names so that "Painting & Drywall",
# "Painting and Drywall", and "Painting an Drywall" all normalise identically.
_COMPANY_STOP_WORDS: frozenset[str] = frozenset({"and", "an", "the", "of"})

# Two-letter US state abbreviations (used to strip trailing location codes)
_US_STATE_CODES: frozenset[str] = frozenset({
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
    "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
})

def _load_company_abbreviations() -> dict[str, str]:
    """Organization-specific name expansions that fuzzy matching can't resolve on its own.

    These are your own clients' names, so they live outside the code: a JSON file of
    ``{"variant": "canonical name"}`` named by ``MSP_COMPANY_ALIASES_FILE``, e.g.
    ``{"acme": "acme plumbing and heating", "a c m e plumbing": "acme plumbing and heating"}``.
    Keys and values are compared lower-case. Ongoing merges can also be made in the app
    (company aliases), which are stored in the database.
    """
    path = os.environ.get("MSP_COMPANY_ALIASES_FILE", "").strip()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


_COMPANY_ABBREVIATIONS: dict[str, str] = _load_company_abbreviations()

# Common business name prefixes to strip (handled after abbreviation check)
# NOTE: DO NOT include "lc" here - it's often part of the actual company name
_COMPANY_PREFIXES: tuple[str, ...] = (
    "dr",
    "the",
)

# Serials in this set are invalid / placeholder values
SERIAL_BLOCKLIST: set[str] = {
    "",
    "NONE",
    "00000000",
    "TO BE FILLED",
    "DEFAULT",
    "N/A",
    "NA",
}


@functools.lru_cache(maxsize=50_000)
def normalize_company_name(name: str) -> str:
    """Return a normalised company name for fuzzy comparison.

    Pure function of ``name`` — cached, because matching calls it millions of times with the same names.

    Steps:
    1. Strip leading/trailing whitespace and convert to lowercase.
    2. Check manual abbreviation mappings for exact expansion.
    3. Remove punctuation.
    4. Remove common legal suffixes (e.g. Inc, Ltd, LLC).
    5. Remove stop words (and, an, the, of).
    6. Strip common business prefixes (dr, lc, the).
    7. Strip trailing US state abbreviation (e.g. "mn", "tx").
    8. Collapse internal whitespace.
    """
    if not name:
        return ""

    result = name.strip().lower()

    # Check manual abbreviation mappings first (before any transformation)
    # This handles cases like "acme" → "acme plumbing and heating" (from MSP_COMPANY_ALIASES_FILE)
    if result in _COMPANY_ABBREVIATIONS:
        result = _COMPANY_ABBREVIATIONS[result]
    # Replace "&" with "and" so "Painting & Drywall" matches "Painting and Drywall"
    result = result.replace("&", " and ")
    # Remove remaining punctuation and underscores, keeping only letters, digits, spaces
    result = re.sub(r"[^a-z0-9\s]", " ", result)
    # Collapse whitespace
    result = " ".join(result.split())

    # Strip legal suffixes iteratively (one pass is enough for well-formed names)
    for suffix in _LEGAL_SUFFIXES:
        pattern = r"\b" + re.escape(suffix) + r"\b"
        result = re.sub(pattern, "", result)

    # Strip common stop words ("and", "an", "the", "of") so truncated variants
    # like "Painting an Drywall" and "Painting and Drywall" converge.
    words = result.split()
    words = [w for w in words if w not in _COMPANY_STOP_WORDS]
    result = " ".join(words)

    # Collapse again after suffix/stop-word removal
    result = " ".join(result.split())

    # Strip common business name prefixes (e.g., "dr", "lc", "the")
    # This handles cases like "Dr Smith Dental" → "smith dental"
    words = result.split()
    if len(words) >= 2 and words[0] in _COMPANY_PREFIXES:
        result = " ".join(words[1:])

    # Strip trailing US state abbreviation if the name has enough words
    words = result.split()
    if len(words) >= 2 and words[-1] in _US_STATE_CODES:
        result = " ".join(words[:-1])

    # Strip trailing single-character fragments (e.g. "l" from ASIO truncating
    # "LLP" to "L", or stray initials left after suffix removal).
    words = result.split()
    while len(words) >= 2 and len(words[-1]) == 1:
        words.pop()
    result = " ".join(words)

    return result


# ── Prefix-based deduplication ────────────────────────────────────

_PREFIX_WORD_COUNT = 3  # minimum shared leading words to consider a match


def merge_prefix_duplicates(
    company_map: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Merge company entries whose first N words are identical.

    When an API truncates a name (e.g. "new franklin r 1 school distri"
    vs "new franklin r 1 school district"), both normalised names share
    the same leading words.  This function groups them by a prefix key
    (first ``_PREFIX_WORD_COUNT`` words) and folds shorter names into
    the longest variant, summing device counts.

    Returns a **new** dict; the original is not mutated.
    """
    # Group names by their first N words
    prefix_groups: dict[str, list[str]] = {}
    for name in company_map:
        words = name.split()
        key = " ".join(words[:_PREFIX_WORD_COUNT]) if len(words) >= _PREFIX_WORD_COUNT else name
        prefix_groups.setdefault(key, []).append(name)

    merged: dict[str, dict[str, int]] = {}
    for _prefix, names in prefix_groups.items():
        if len(names) == 1:
            merged[names[0]] = company_map[names[0]]
            continue
        # Pick the longest name as canonical
        canonical = max(names, key=len)
        combined: dict[str, int] = {}
        for n in names:
            for platform, count in company_map[n].items():
                combined[platform] = combined.get(platform, 0) + count
        merged[canonical] = combined
    return merged


def resolve_with_prefix(
    name: str,
    canonical_names: list[str],
) -> str:
    """Return the longest canonical name that shares the same prefix, or *name* itself.

    Used by the unmatched-devices endpoint so truncated company names
    resolve to the same canonical entry produced by :func:`merge_prefix_duplicates`.
    """
    words = name.split()
    if len(words) < _PREFIX_WORD_COUNT:
        return name
    prefix = " ".join(words[:_PREFIX_WORD_COUNT])
    best = name
    for candidate in canonical_names:
        if candidate.split()[:_PREFIX_WORD_COUNT] == words[:_PREFIX_WORD_COUNT]:
            if len(candidate) > len(best):
                best = candidate
    return best


def normalize_hostname(hostname: str) -> str:
    """Return the short hostname in uppercase, stripping any FQDN suffix."""
    if not hostname:
        return ""
    short = hostname.split(".")[0]
    return short.strip().upper()


def hostname_match_key(hostname: str) -> str:
    """Return an alphanumeric-only skeleton of *hostname* for fuzzy matching.

    This strips smart-quotes, apostrophes, hyphens, spaces, underscores and
    any other non-alphanumeric characters so that hostnames like
    ``FRONTS-IMAC-SYDNEE`` and ``FRONT\u2019S IMAC-SYDNEE`` both produce
    ``FRONTSIMACSYDNEE``.

    The input should already be the output of :func:`normalize_hostname`
    (i.e. uppercase, FQDN stripped).
    """
    if not hostname:
        return ""
    # Remove every character that is not ASCII letter or digit
    return re.sub(r"[^A-Z0-9]", "", hostname.upper())


def normalize_mac(mac: str) -> str:
    """Return a MAC address with separators stripped and characters uppercased."""
    if not mac:
        return ""
    cleaned = re.sub(r"[:\-\.\s]", "", mac)
    return cleaned.upper()


def normalize_serial(serial: str) -> str:
    """Return a normalised serial number, or empty string if it is a placeholder.

    Checks the raw uppercase value against :data:`SERIAL_BLOCKLIST` first
    (to catch multi-word placeholders like "TO BE FILLED"), then strips
    hyphens and spaces.
    """
    if not serial:
        return ""
    stripped = serial.strip()
    if not stripped:
        return ""
    # Check against blocklist using the uppercased raw value (before removing spaces)
    upper_raw = stripped.upper()
    if upper_raw in SERIAL_BLOCKLIST:
        return ""
    cleaned = re.sub(r"[\-\s]", "", stripped).upper()
    if cleaned in SERIAL_BLOCKLIST:
        return ""
    return cleaned
