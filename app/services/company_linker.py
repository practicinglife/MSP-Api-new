"""Company name linking: which names in different tools are the same client, and why.

Every tool spells clients its own way — ``Timmerman WFP`` in SentinelOne, ``Timmermann Wildland Fire Servi``
(cut off at 30 characters) in Asio, ``Timmermann Wildland Fire Services`` in PSA. Normalisation
(:func:`app.services.normalizers.normalize_company_name`) removes case, punctuation and legal suffixes;
this module decides the rest from two kinds of evidence and explains each decision in plain words:

* **Devices** — the same computer (hostname, serial number or endpoint ID) reported under both names by
  different tools. Hostnames and serials that show up under many unrelated companies (``DC01``,
  ``To be filled by O.E.M.``) are ignored.
* **Names** — one name cut off from the other, one name's words leading the other's, initials
  (``AOCR`` → ``American Osteopathic College of Radiology``), or close spelling.

Each pair gets a score 0–100. At :data:`AUTO_LINK_SCORE` and above the link is made automatically (and
shown, with its reason, so it can be undone); from :data:`SUGGEST_SCORE` it is offered for one-click review.
Pure functions only: callers load the inputs and store the results.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.services.normalizers import (
    hostname_match_key,
    normalize_company_name,
    normalize_hostname,
    normalize_serial,
)

AUTO_LINK_SCORE = 90
SUGGEST_SCORE = 60

# Higher-priority tools' names become the canonical name when neither name is a cut-off of the other.
PLATFORM_PRIORITY = ("connectwise_psa", "asio", "sentinelone", "screenconnect", "eset", "avast")

# Hostnames reused by many unrelated companies; never evidence on their own.
GENERIC_HOSTNAMES = frozenset({
    "DC", "DC01", "DC02", "DC03", "DC1", "DC2", "DC3", "PDC", "BDC", "AD01", "AD02", "AD1", "AD2",
    "RDP", "RDP1", "RDP2", "RDP01", "RDP02", "RDS", "RDS01", "RDS02", "RDS1", "RDS2",
    "TS", "TS01", "TS02", "TS1", "TS2", "TERMINAL", "TERMSERV", "REMOTE",
    "SERVER", "SERVER01", "SERVER02", "SERVER03", "SERVER1", "SERVER2", "SERVER3",
    "SRV", "SRV01", "SRV02", "SRV1", "SRV2", "HOST", "HOST01", "HOST02",
    "FILESERVER", "FS", "FS01", "FS02", "FS1", "PRINTSERVER", "PRINT", "PRINT01",
    "NAS", "NAS01", "NAS1", "EXCHANGE", "EXCH", "EXCH01", "MAIL", "MAIL01", "MAIL1",
    "PC", "PC01", "PC02", "PC1", "PC2", "DESKTOP", "DESKTOP01", "DESKTOP1",
    "WORKSTATION", "WS01", "WS1", "LAPTOP", "LAPTOP01", "LAPTOP1", "RECEPTION", "FRONTDESK", "ADMIN",
    "FW", "FW01", "FIREWALL", "SWITCH", "SWITCH01", "ROUTER", "AP", "AP01", "VPN", "VPN01",
    "HYPERV", "HYPERV01", "ESXI", "ESXI01", "VMHOST", "VMHOST01",
    "BACKUP", "BACKUP01", "BAK", "BAK01", "SQL", "SQL01", "SQL02", "SQL1", "SQL2", "DB", "DB01", "DB02", "DB1",
})
# Placeholder serials that BIOS vendors ship (normalize_serial already drops the shortest ones).
PLACEHOLDER_SERIALS = frozenset({
    "TOBEFILLEDBYOEM", "TOBEFILLEDBYO.E.M.", "SYSTEMSERIALNUMBER", "DEFAULTSTRING", "CHASSISSERIALNUMBER",
    "0", "0123456789", "123456789", "1234567890", "INVALID", "NOTAPPLICABLE", "NOTSPECIFIED", "UNKNOWN",
})
# An identifier seen under more companies than this is shared equipment or noise, not evidence.
MAX_COMPANIES_PER_IDENTIFIER = 2
_MIN_TRUNCATION_LEN = 8


@dataclass(frozen=True)
class DeviceEvidence:
    """One device as a tool reports it."""

    platform: str
    company: str  # raw company / site name from the tool
    hostname: str = ""
    serial: str = ""
    endpoint_id: str = ""


@dataclass
class LinkSuggestion:
    """``name`` (a normalised company name) is probably the same client as ``target``."""

    name: str
    target: str
    score: int
    reasons: list[str]
    shared_devices: list[str] = field(default_factory=list)
    name_platforms: list[str] = field(default_factory=list)
    target_platforms: list[str] = field(default_factory=list)

    @property
    def auto(self) -> bool:
        return self.score >= AUTO_LINK_SCORE

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "target": self.target, "score": self.score, "auto": self.auto,
                "reason": self.reason, "reasons": self.reasons, "shared_devices": self.shared_devices,
                "name_platforms": self.name_platforms, "target_platforms": self.target_platforms}


# ── Name evidence ─────────────────────────────────────────────────────────────────────────────────


def _initials(words: list[str]) -> str:
    return "".join(w[0] for w in words if w)


def name_evidence(a: str, b: str) -> tuple[int, str]:
    """Score how alike two *normalised* names are, with the reason. (0, "") when they aren't."""
    if not a or not b or a == b:
        return 0, ""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    sw, lw = short.split(), long_.split()
    # Spacing only: "l c betz jewelers" / "lc betz jewelers"
    if short.replace(" ", "") == long_.replace(" ", ""):
        return 99, f'"{short}" and "{long_}" differ only in spacing'
    # Cut off mid-word: "timmermann wildland fire servi" is the start of "timmermann wildland fire services"
    if len(short) >= _MIN_TRUNCATION_LEN and long_.startswith(short) and len(sw) >= 2             and long_[len(short)] != " ":
        return 97, f'"{short}" is "{long_}" cut off'
    # Leading words: "simmons henderson" → "simmons henderson associates"
    if len(sw) >= 2 and lw[: len(sw)] == sw:
        return 85, f'"{long_}" starts with "{short}"'
    # Initials: "aocr" → "american osteopathic college radiology"; "little dixie fpd" → "… fire protection district"
    if len(sw) == 1 and 2 <= len(short) <= 6 and len(lw) >= 2 and _initials(lw) == short:
        return 85, f'"{short}" is the initials of "{long_}"'
    if len(sw) >= 2 and len(sw[-1]) >= 2 and lw[: len(sw) - 1] == sw[:-1] \
            and _initials(lw[len(sw) - 1:]) == sw[-1]:
        return 85, f'"{short}" abbreviates "{long_}"'
    score = int(round(fuzz.token_sort_ratio(a, b)))
    if score >= 80:
        return score, f"names are {score}% alike"
    return 0, ""


# ── Device evidence ───────────────────────────────────────────────────────────────────────────────


def _device_keys(dev: DeviceEvidence) -> list[tuple[str, str]]:
    """Identifiers that mean "same physical computer", as (kind, value)."""
    keys: list[tuple[str, str]] = []
    host = hostname_match_key(normalize_hostname(dev.hostname))
    if host and host not in GENERIC_HOSTNAMES and len(host) >= 3:
        keys.append(("host", host))
    serial = normalize_serial(dev.serial)
    if serial and serial.replace(".", "") not in PLACEHOLDER_SERIALS and len(serial) >= 5:
        keys.append(("serial", serial))
    if dev.endpoint_id and len(dev.endpoint_id) >= 8:
        keys.append(("endpoint", dev.endpoint_id.strip().lower()))
    return keys


@dataclass
class _Company:
    name: str
    platforms: set[str] = field(default_factory=set)
    devices: int = 0
    # (kind, value) → hostname shown to people
    keys: dict[tuple[str, str], str] = field(default_factory=dict)


def _collect(devices: Iterable[DeviceEvidence], company_names: Iterable[tuple[str, str]],
             aliases: dict[str, str]) -> dict[str, _Company]:
    companies: dict[str, _Company] = {}

    def company(raw: str) -> _Company | None:
        norm = normalize_company_name(raw)
        if not norm:
            return None
        norm = aliases.get(norm, norm)
        return companies.setdefault(norm, _Company(norm))

    for platform, raw in company_names:
        c = company(raw)
        if c:
            c.platforms.add(platform)
    for dev in devices:
        c = company(dev.company)
        if not c:
            continue
        c.platforms.add(dev.platform)
        c.devices += 1
        label = normalize_hostname(dev.hostname) or dev.serial or dev.endpoint_id
        for key in _device_keys(dev):
            c.keys.setdefault((dev.platform, *key), label)  # type: ignore[arg-type]
    return companies


def _shared(a: _Company, b: _Company) -> list[str]:
    """Hostnames of computers reported under ``a`` by one tool and under ``b`` by a different tool."""
    a_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (platform, kind, value) in a.keys:  # type: ignore[misc]
        a_keys[(kind, value)].add(platform)
    shown: dict[str, None] = {}
    for (platform, kind, value), label in b.keys.items():  # type: ignore[misc]
        plats = a_keys.get((kind, value))
        if plats and (plats - {platform}):
            shown[label] = None
    return list(shown)


def _canonical(a: _Company, b: _Company) -> tuple[_Company, _Company]:
    """(canonical, variant): the longer name when one is cut off from the other, else the higher-priority tool."""
    if b.name.startswith(a.name):
        return b, a
    if a.name.startswith(b.name):
        return a, b

    def rank(c: _Company) -> tuple[int, int, int]:
        best = min((PLATFORM_PRIORITY.index(p) for p in c.platforms if p in PLATFORM_PRIORITY), default=99)
        return best, -len(c.platforms), -c.devices

    return (a, b) if rank(a) <= rank(b) else (b, a)


def _score(name_score: int, name_reason: str, shared: list[str], a: _Company, b: _Company) -> tuple[int, list[str]]:
    reasons: list[str] = []
    n = len(shared)
    if n:
        listed = ", ".join(shared[:3]) + (f" +{n - 3} more" if n > 3 else "")
        reasons.append(f"{n} shared computer{'s' if n != 1 else ''}: {listed}")
    if name_reason:
        reasons.append(name_reason)
    # Share of the smaller company's computers that are also under the other name.
    smaller = max(1, min(len(set(a.keys.values())), len(set(b.keys.values()))))
    if n >= 2 and n / smaller >= 0.3:
        score = 95 if name_score else 92
    elif n >= 1 and name_score >= 60:
        score = 93
    elif n >= 2 or (n == 1 and n / smaller >= 0.5):
        # Some shared computers and nothing else: worth a look, not proof (a tech's laptop, a moved PC).
        score = 65
    elif n == 1:
        score = 0  # one computer among many: almost always a tech's machine or a reused name
    else:
        score = name_score if name_score >= 95 else min(name_score, 89)
    return score, reasons


def resolve_aliases(aliases: dict[str, str]) -> dict[str, str]:
    """Follow alias chains (a → b → c becomes a → c), ignoring loops."""
    out: dict[str, str] = {}
    for name in aliases:
        seen, target = {name}, aliases[name]
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        if target != name:
            out[name] = target
    return out


def suggest_links(
    devices: Iterable[DeviceEvidence],
    company_names: Iterable[tuple[str, str]] = (),
    aliases: dict[str, str] | None = None,
    rejected: set[tuple[str, str]] | None = None,
    ignored: set[str] | None = None,
) -> list[LinkSuggestion]:
    """Find names that are probably the same client.

    ``devices`` — every device from every tool. ``company_names`` — (platform, raw name) for companies that
    may have no devices. ``aliases`` — links already made (normalised name → canonical), applied first.
    ``rejected`` — (name, target) pairs a person said are different. ``ignored`` — names marked as not a
    managed client. Returns at most one suggestion per name (its best), highest score first.

    Runs in rounds: automatic links from one round are applied before the next, so a client spelled three
    ways ("timmerman wfp", "… fire servi", "… fire services") collapses step by step instead of its
    computers looking like equipment shared between three companies.
    """
    devices = list(devices)
    company_names = list(company_names)
    current = resolve_aliases(dict(aliases or {}))
    found: dict[str, LinkSuggestion] = {}
    for _round in range(4):
        new_auto = 0
        for s in _suggest_once(devices, company_names, current, rejected, ignored):
            if s.name in current:
                continue
            found[s.name] = s
            if s.auto:
                current[s.name] = s.target
                new_auto += 1
        current = resolve_aliases(current)
        # Review suggestions for names that have since been linked automatically are stale.
        found = {n: s for n, s in found.items() if s.auto or n not in current}
        if not new_auto:
            break
    for s in found.values():  # point every link at where its target ends up
        final = current.get(s.target, s.target)
        if final != s.target:
            s.reasons.append(f'"{s.target}" is in turn linked to "{final}"')
            s.target = final
    return sorted((s for s in found.values() if s.name != s.target), key=lambda s: (-s.score, s.name))


def _suggest_once(
    devices: Iterable[DeviceEvidence],
    company_names: Iterable[tuple[str, str]] = (),
    aliases: dict[str, str] | None = None,
    rejected: set[tuple[str, str]] | None = None,
    ignored: set[str] | None = None,
) -> list[LinkSuggestion]:
    aliases = aliases or {}
    rejected = rejected or set()
    ignored = ignored or set()
    companies = _collect(devices, company_names, aliases)

    # Identifiers seen under too many companies are shared equipment or junk — drop them.
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for c in companies.values():
        for (_platform, kind, value) in c.keys:  # type: ignore[misc]
            owners[(kind, value)].add(c.name)
    noisy = {k for k, names in owners.items() if len(names) > MAX_COMPANIES_PER_IDENTIFIER}
    for c in companies.values():
        c.keys = {k: v for k, v in c.keys.items() if k[1:] not in noisy}  # type: ignore[index]

    # Candidate pairs: any shared identifier, or names that could be related.
    pairs: set[tuple[str, str]] = set()
    for names in owners.values():
        if 2 <= len(names) <= MAX_COMPANIES_PER_IDENTIFIER:
            a, b = sorted(names)
            pairs.add((a, b))
    names_sorted = sorted(companies)
    for i, a in enumerate(names_sorted):
        for b in names_sorted[i + 1:]:
            if (a, b) not in pairs and name_evidence(a, b)[0]:
                pairs.add((a, b))

    best: dict[str, LinkSuggestion] = {}
    for a_name, b_name in pairs:
        a, b = companies[a_name], companies[b_name]
        if a_name in ignored or b_name in ignored:
            continue
        if a.platforms and a.platforms == b.platforms and len(a.platforms) == 1:
            continue  # both only in the same tool: two companies there, not one client spelled two ways
        canonical, variant = _canonical(a, b)
        if (variant.name, canonical.name) in rejected:
            continue
        name_score, name_reason = name_evidence(a_name, b_name)
        shared = _shared(a, b)
        score, reasons = _score(name_score, name_reason, shared, a, b)
        if score < SUGGEST_SCORE:
            continue
        s = LinkSuggestion(variant.name, canonical.name, score, reasons, shared,
                           sorted(variant.platforms), sorted(canonical.platforms))
        current = best.get(variant.name)
        if current is None or s.score > current.score:
            best[variant.name] = s

    # One round links a name only once; a target that is itself being linked waits for the next round.
    return [s for s in best.values() if not (s.target in best and best[s.target].auto)]


def unmatched_names(
    devices: Iterable[DeviceEvidence],
    company_names: Iterable[tuple[str, str]] = (),
    aliases: dict[str, str] | None = None,
    platforms_expected: int = 2,
) -> list[dict[str, object]]:
    """Names found in fewer than ``platforms_expected`` tools after links are applied."""
    companies = _collect(devices, company_names, resolve_aliases(aliases or {}))
    out = [{"name": c.name, "platforms": sorted(c.platforms), "devices": c.devices}
           for c in companies.values() if len(c.platforms) < platforms_expected]
    return sorted(out, key=lambda r: (-int(r["devices"]), str(r["name"])))  # type: ignore[call-overload]


def all_company_names(
    devices: Iterable[DeviceEvidence],
    company_names: Iterable[tuple[str, str]] = (),
    aliases: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Every company after links, for pickers: name, tools, device count."""
    companies = _collect(devices, company_names, resolve_aliases(aliases or {}))
    return sorted(({"name": c.name, "platforms": sorted(c.platforms), "devices": c.devices}
                   for c in companies.values()), key=lambda r: str(r["name"]))


def rank_candidates(
    devices: Iterable[DeviceEvidence],
    name: str,
    candidates: Iterable[str],
    aliases: dict[str, str] | None = None,
    limit: int = 5,
) -> list[dict[str, object]]:
    """The ``candidates`` most likely to be the same client as ``name`` (normalised), best first, each with a
    score and reason — for a "which company is this?" dropdown. Weak matches are included (score > 0) so the
    list is useful even when nothing is certain."""
    return Ranker(devices, aliases).rank(name, candidates, limit)


class Ranker:
    """:func:`rank_candidates` for many names over the same devices (collects the evidence once)."""

    def __init__(self, devices: Iterable[DeviceEvidence], aliases: dict[str, str] | None = None) -> None:
        self.companies = _collect(devices, (), resolve_aliases(aliases or {}))

    def rank(self, name: str, candidates: Iterable[str], limit: int = 5) -> list[dict[str, object]]:
        companies = self.companies
        me = companies.get(name) or _Company(name)
        out: list[dict[str, object]] = []
        for cand in set(candidates):
            if cand == name:
                continue
            other = companies.get(cand) or _Company(cand)
            name_score, name_reason = name_evidence(name, cand)
            shared = _shared(me, other)
            score, reasons = _score(name_score, name_reason, shared, me, other)
            if not score:
                # still rank by spelling so the dropdown isn't empty
                score = int(round(fuzz.WRatio(name, cand) * 0.5))
                reasons = []
            if score > 0:
                out.append({"name": cand, "score": score, "reason": "; ".join(reasons), "shared_devices": shared,
                            "platforms": sorted(other.platforms)})
        out.sort(key=lambda r: (-int(r["score"]), str(r["name"])))  # type: ignore[call-overload]
        return out[:limit]
