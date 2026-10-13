"""Client naming standard — pure functions, no I/O.

Standard Name: <= 30 chars (Asio truncates beyond that), letters/digits/spaces/hyphen,
no legal suffix, ``&`` spelled ``and``.  Client Code: 2-4 upper-case letters, unique.
"""

from __future__ import annotations

import re

STANDARD_NAME_MAX = 30
CLIENT_CODE_RE = re.compile(r"^[A-Z]{2,4}$")
_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 -]+$")
_LEGAL_SUFFIX_RE = re.compile(r"[\s,_-]+(llc|l\.l\.c\.|inc|llp|pc|p\.c\.|co|corp|ltd)\.?$", re.I)
_CODE_STOP_WORDS = {"and", "of", "the", "for", "a", "an"}
_GENERIC_CODES = {"MAIN", "NUC", "LAP", "LAT", "DESK", "SRV", "PC", "VM", "HOST", "TEMP"}


def clean_standard_name(raw: str) -> str:
    """Turn a platform's raw company name into a Standard Name candidate.

    Undoes the punctuation mangling each tool applies (Asio ``_``, ScreenConnect ``-``),
    spells ``&`` as ``and``, drops legal suffixes and disallowed characters.
    Does **not** truncate — over-length names must be shortened by a human.
    """
    if not raw:
        return ""
    name = raw.strip()
    name = name.replace("&", " and ")
    # Asio replaces & , . ' with "_": a standalone "_" was "&", "word_" was "word,".
    name = re.sub(r"\s_\s", " and ", name)
    name = re.sub(r"(\w)_ ", r"\1 ", name)
    name = name.replace("_", " ")
    # ScreenConnect replaces the same characters with "-".
    sc_mangled = bool(re.search(r"\w- ", name))
    name = re.sub(r"\b([A-Za-z])-(?=[A-Za-z]-)", r"\1", name)  # "A-B- Smith" -> "AB- Smith"
    name = re.sub(r"(\w)- ", r"\1 ", name)                     # "Gerding- Korte" -> "Gerding Korte"
    if sc_mangled:
        name = re.sub(r"\s-\s", " and ", name)
    name = re.sub(r"\s+", " ", name).strip(" ,.-")
    prev = None
    while prev != name:
        prev = name
        name = _LEGAL_SUFFIX_RE.sub("", name).strip(" ,.-")
    name = re.sub(r"[^A-Za-z0-9 -]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -")
    name = re.sub(r"\band\s+and\b", "and", name, flags=re.I)
    return name


def validate_standard_name(name: str) -> list[str]:
    """Return a list of naming-standard violations (empty list = compliant)."""
    problems: list[str] = []
    if not name or not name.strip():
        return ["empty"]
    if len(name) > STANDARD_NAME_MAX:
        problems.append(f"longer than {STANDARD_NAME_MAX} characters ({len(name)})")
    if not _ALLOWED_RE.match(name):
        bad = sorted({c for c in name if not re.match(r"[A-Za-z0-9 -]", c)})
        problems.append("disallowed characters: " + " ".join(bad))
    if name != name.strip() or "  " in name:
        problems.append("extra whitespace")
    if _LEGAL_SUFFIX_RE.search(name):
        problems.append("legal suffix")
    return problems


def validate_client_code(code: str) -> list[str]:
    if not code:
        return ["empty"]
    if not CLIENT_CODE_RE.match(code):
        return ["must be 2-4 upper-case letters"]
    if code in _GENERIC_CODES:
        return ["generic word, not a client code"]
    return []


def suggest_client_code(standard_name: str, taken: set[str] | None = None) -> str:
    """Initials-based code, made unique against *taken*. Empty string if impossible."""
    taken = taken or set()
    words = [w for w in re.findall(r"[A-Za-z0-9]+", standard_name) if w.lower() not in _CODE_STOP_WORDS]
    if not words:
        return ""
    letters_only = [re.sub(r"[^A-Za-z]", "", w) for w in words]
    letters_only = [w for w in letters_only if w]
    if not letters_only:
        return ""
    candidates: list[str] = []
    if len(letters_only) == 1:
        w = letters_only[0].upper()
        candidates += [w[:4], w[:3], w[:2]]
    else:
        initials = "".join(w[0] for w in letters_only).upper()
        candidates.append(initials[:4])
        # first word gets a second letter: "Badco Inc" -> "BA", "Wade Stables" -> "WAS"
        first = letters_only[0].upper()
        rest = "".join(w[0] for w in letters_only[1:]).upper()
        candidates.append((first[:2] + rest)[:4])
        candidates.append((first[:3] + rest)[:4])
    for c in candidates:
        if len(c) >= 2 and c not in taken and c not in _GENERIC_CODES:
            return c
    base = candidates[0][:3]
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        c = (base + ch)[:4]
        if c not in taken:
            return c
    return ""


def names_equivalent(a: str, b: str) -> bool:
    """True when two display names are the same per the naming standard (case-insensitive, trimmed)."""
    return (a or "").strip().casefold() == (b or "").strip().casefold()
