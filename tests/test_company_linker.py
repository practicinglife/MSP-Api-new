"""Company name linking: the cases that used to need hand-made aliases now link themselves, with a reason."""

import asyncio

import pytest

from app.services.company_linker import DeviceEvidence, name_evidence, suggest_links, unmatched_names

D = DeviceEvidence


def _by_name(suggestions):  # type: ignore[no-untyped-def]
    return {s.name: s for s in suggestions}


@pytest.mark.parametrize(("a", "b", "low"), [
    ("Hartwell Wildland Fire Servi", "Hartwell Wildland Fire Services", 95),   # cut off at 30 characters
    ("Pine Ridge Dental", "Pine Ridge Dental Associates", 85),                  # leading words
    ("ACVR", "American College of Veterinary Radiology", 85),                   # initials
    ("Lake Ozark FPD", "Lake Ozark Fire Protection District", 85),               # partial initials
    ("L C Moss Jewelers", "LC Moss Jewelers", 99),                               # spacing only
])
def test_name_evidence(a, b, low):  # type: ignore[no-untyped-def]
    from app.services.normalizers import normalize_company_name as n

    score, reason = name_evidence(n(a), n(b))
    assert score >= low and reason


def test_unrelated_names_have_no_name_evidence():
    assert name_evidence("acme plumbing", "zenith dental")[0] == 0


def test_truncated_name_links_automatically_without_devices():
    s = _by_name(suggest_links([], company_names=[
        ("asio", "Hartwell Wildland Fire Servi"), ("connectwise_psa", "Hartwell Wildland Fire Services")]))
    link = s["hartwell wildland fire servi"]
    assert link.auto and link.target == "hartwell wildland fire services" and "cut off" in link.reason


def test_shared_computers_link_an_abbreviation():
    devices = [D("connectwise_psa", "Northgate Animal Health", f"NAH-PC{i}") for i in range(4)]
    devices += [D("sentinelone", "NGAH", f"NAH-PC{i}") for i in range(3)]
    link = _by_name(suggest_links(devices))["ngah"]
    assert link.auto and link.target == "northgate animal health"
    assert "3 shared computers" in link.reason and link.shared_devices[:1] == ["NAH-PC0"]


def test_one_shared_computer_plus_similar_name_links():
    devices = [D("connectwise_psa", "Pine Ridge Dental Associates", f"PRDA-{i}") for i in range(5)]
    devices += [D("sentinelone", "Pine Ridge Dental", "PRDA-1")]
    link = _by_name(suggest_links(devices))["pine ridge dental"]
    assert link.auto and link.target == "pine ridge dental associates"


def test_three_spellings_collapse_across_rounds():
    """Two computers under three spellings looked like shared equipment until the first link was made."""
    devices = [D("connectwise_psa", "Hartwell Wildland Fire Services", h) for h in ("HWF-JOHN", "HWF-OFFICE")]
    devices += [D("asio", "Hartwell Wildland Fire Servi", h) for h in ("HWF-JOHN", "HWF-OFFICE")]
    devices += [D("sentinelone", "Hartwell WFP", h) for h in ("HWF-JOHN", "HWF-OFFICE")]
    s = _by_name(suggest_links(devices))
    assert s["hartwell wildland fire servi"].auto
    assert s["hartwell wfp"].target == "hartwell wildland fire services" and s["hartwell wfp"].auto


def test_generic_and_widely_shared_hostnames_are_not_evidence():
    devices = []
    for company in ("Acme Plumbing", "Zenith Dental", "Bluebird Cafe"):
        devices += [D("asio", company, "DC01"), D("asio", company, "FRONT-DESK-PC")]
    devices += [D("sentinelone", "Orchard Law", "DC01"), D("sentinelone", "Orchard Law", "FRONT-DESK-PC")]
    assert suggest_links(devices) == []


def test_one_shared_computer_among_many_is_ignored():
    devices = [D("connectwise_psa", "Our MSP", f"TECH-{i}") for i in range(20)]
    devices += [D("sentinelone", "Orchard Law", "TECH-3")] + [D("sentinelone", "Orchard Law", f"OL-{i}") for i in range(9)]
    assert suggest_links(devices) == []


def test_names_only_in_the_same_tool_are_not_linked():
    devices = [D("connectwise_psa", "Acme Plumbing", f"PC{i}X") for i in range(5)]
    devices += [D("connectwise_psa", "Acme Plumbing Supply", f"PC{i}X") for i in range(5)]
    assert suggest_links(devices) == []


def test_rejected_and_ignored_are_respected():
    names = [("asio", "Hartwell Wildland Fire Servi"), ("connectwise_psa", "Hartwell Wildland Fire Services")]
    assert suggest_links([], names, rejected={("hartwell wildland fire servi", "hartwell wildland fire services")}) == []
    assert suggest_links([], names, ignored={"hartwell wildland fire servi"}) == []


def test_manual_alias_is_applied_first():
    names = [("sentinelone", "Zephyr"), ("connectwise_psa", "Blue River Credit Fund")]
    assert suggest_links([], names, aliases={"zephyr": "blue river credit fund"}) == []
    assert unmatched_names([], names, aliases={"zephyr": "blue river credit fund"}) == []


def test_unmatched_lists_single_tool_names():
    names = [("sentinelone", "Zephyr"), ("connectwise_psa", "Acme"), ("asio", "Acme")]
    assert [u["name"] for u in unmatched_names([], names)] == ["zephyr"]


# ── Stored: automatic links, decisions, API ──────────────────────────────────────────────────────────


def _seed_devices(factory, rows):  # type: ignore[no-untyped-def]
    from uuid import uuid4

    from app.db.schema import PlatformDevice
    from app.services.normalizers import normalize_company_name

    async def _add() -> None:
        async with factory() as db:
            for platform, company, host in rows:
                db.add(PlatformDevice(id=str(uuid4()), platform=platform, platform_id=str(uuid4()), endpoint_id="",
                                      hostname=host, hostname_norm=host, company_raw=company,
                                      company_canonical=normalize_company_name(company), serial_number="",
                                      mac_addresses="", raw_json={}))
            await db.commit()

    asyncio.run(_add())


def _canonicals(factory):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.db.schema import PlatformDevice

    async def _get():  # type: ignore[no-untyped-def]
        async with factory() as db:
            return {r for r in (await db.execute(select(PlatformDevice.company_canonical))).scalars()}

    return asyncio.run(_get())


def test_auto_links_are_stored_with_reason_and_undo_sticks(db_factory):  # type: ignore[no-untyped-def]
    from app.services import company_matching as cm

    rows = [("connectwise_psa", "Northgate Animal Health", f"NAH-PC{i}") for i in range(3)]
    rows += [("sentinelone", "NGAH", f"NAH-PC{i}") for i in range(3)]
    _seed_devices(db_factory, rows)

    async def run():  # type: ignore[no-untyped-def]
        async with db_factory() as s:
            return await cm.overview(s)

    view = asyncio.run(run())
    auto = [link for link in view["links"] if link["source"] == "auto"]
    assert [(a["name"], a["target"]) for a in auto] == [("ngah", "northgate animal health")]
    assert "shared computers" in auto[0]["reason"]
    assert _canonicals(db_factory) == {"northgate animal health"}  # devices re-pointed at once

    async def undo():  # type: ignore[no-untyped-def]
        async with db_factory() as s:
            assert await cm.unlink(s, "ngah", by="tester")
        async with db_factory() as s:
            return await cm.overview(s)

    view = asyncio.run(undo())
    assert view["links"] == [] and _canonicals(db_factory) == {"northgate animal health", "ngah"}


def test_review_link_not_managed_via_api(app_client, db_factory):  # type: ignore[no-untyped-def]
    from tests.conftest import make_session

    rows = [("connectwise_psa", "Blue River Credit Fund", "BRCF-1"), ("asio", "Blue River Credit Fund", "BRCF-1"),
            ("sentinelone", "Zephyr", "ZP-LAPTOP"), ("connectwise_psa", "Jan Smith", "JS-PC")]
    _seed_devices(db_factory, rows)
    cookie = make_session(db_factory, ["MSPTools.User", "MSPTools.Editor"])
    app_client.cookies.set("msp_session", cookie)
    headers = {"X-CSRF-Token": "csrf-token"}

    view = app_client.get("/api/companies/matching").json()
    assert {u["name"] for u in view["unmatched"]} == {"zephyr", "jan smith"}

    r = app_client.post("/api/companies/matching/link", json={"name": "Zephyr", "target": "Blue River Credit Fund"},
                        headers=headers)
    assert r.status_code == 200, r.text
    r = app_client.post("/api/companies/matching/not-managed", json={"name": "Jan Smith"}, headers=headers)
    assert r.status_code == 200, r.text

    view = app_client.get("/api/companies/matching").json()
    assert view["unmatched"] == [] and view["not_managed"] == ["jan smith"]
    assert [(link["name"], link["source"]) for link in view["links"]] == [("zephyr", "manual")]
    assert "blue river credit fund" in _canonicals(db_factory) and "zephyr" not in _canonicals(db_factory)
