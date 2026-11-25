"""Tests for bloodbank-opt."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from bloodbank_opt import (
    BloodComponent,
    BloodGroup,
    BloodUnit,
    allocate_prbc_request,
    audit_inventory_health,
    is_plasma_compatible,
    is_rbc_compatible,
    match_regional_transfers,
)
from bloodbank_opt.cli import main as cli_main


def test_rbc_and_plasma_immunohematology_compatibility() -> None:
    # O- is universal RBC donor; AB+ is universal RBC recipient
    for g in BloodGroup:
        assert is_rbc_compatible(BloodGroup.O_NEG, g) is True
        assert is_rbc_compatible(g, BloodGroup.AB_POS) is True
    # O- recipient cannot receive O+
    assert is_rbc_compatible(BloodGroup.O_POS, BloodGroup.O_NEG) is False
    # Plasma compatibility is reversed: AB is universal FFP donor; O cannot donate FFP to A
    for g in BloodGroup:
        assert is_plasma_compatible(BloodGroup.AB_POS, g) is True
    assert is_plasma_compatible(BloodGroup.O_NEG, BloodGroup.A_POS) is False
    assert is_plasma_compatible(BloodGroup.A_POS, BloodGroup.O_NEG) is True


def test_o_neg_conservation_guard_and_fefo_ordering() -> None:
    today = date(2026, 11, 25)
    inv = [
        BloodUnit("U-A1", "H1", BloodGroup.A_POS, BloodComponent.PRBC, date(2026, 12, 10)),
        BloodUnit("U-A2", "H1", BloodGroup.A_POS, BloodComponent.PRBC, date(2026, 12, 2)),
        BloodUnit("U-O1", "H1", BloodGroup.O_NEG, BloodComponent.PRBC, date(2026, 12, 1)),
        BloodUnit("U-O2", "H1", BloodGroup.O_NEG, BloodComponent.PRBC, date(2026, 12, 5)),
    ]
    # Request 3 A+ units with O- reserve=2: allocates both A+ (FEFO: U-A2 then U-A1) and protects O-!
    dec = allocate_prbc_request(
        inv,
        recipient_group=BloodGroup.A_POS,
        units_requested=3,
        as_of=today,
        o_neg_emergency_reserve=2,
    )
    assert [u.unit_id for u in dec.allocated_units] == ["U-A2", "U-A1"]
    assert dec.shortfall_units == 1
    assert dec.o_neg_reserve_protected is True

    # With emergency_override=True, O- is released
    emerg = allocate_prbc_request(
        inv,
        recipient_group=BloodGroup.A_POS,
        units_requested=3,
        as_of=today,
        o_neg_emergency_reserve=2,
        emergency_override=True,
    )
    assert len(emerg.allocated_units) == 3
    assert emerg.shortfall_units == 0


def test_regional_hub_transfer_rescues_near_expiry_units() -> None:
    today = date(2026, 11, 25)
    inv = [
        BloodUnit("RURAL-1", "HC4-RURAL", BloodGroup.A_POS, BloodComponent.PRBC, date(2026, 11, 29)),
        BloodUnit("RURAL-2", "HC4-RURAL", BloodGroup.B_POS, BloodComponent.PRBC, date(2026, 12, 20)),
    ]
    transfers = match_regional_transfers(
        inv,
        facility_deficits={"MULAGO-HUB": {BloodGroup.AB_POS: 2}},
        as_of=today,
        surplus_expiry_window_days=7,
    )
    assert len(transfers) == 1
    assert transfers[0].unit_id == "RURAL-1"
    assert transfers[0].to_facility == "MULAGO-HUB"


def test_inventory_health_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "units": [
            {"unit_id": "O1", "facility_id": "H1", "blood_group": "O-", "expiry_date": "2026-12-10"},
            {"unit_id": "O2", "facility_id": "H1", "blood_group": "O-", "expiry_date": "2026-12-12"},
            {"unit_id": "O3", "facility_id": "H1", "blood_group": "O-", "expiry_date": "2026-12-15"},
            {"unit_id": "O4", "facility_id": "H1", "blood_group": "O-", "expiry_date": "2026-12-18"},
            {"unit_id": "A1", "facility_id": "H1", "blood_group": "A+", "expiry_date": "2026-12-08"},
        ]
    }
    in_file = tmp_path / "inv.json"
    in_file.write_text(json.dumps(payload), encoding="utf-8")
    rc = cli_main([str(in_file), "--facility", "H1", "--as-of", "2026-11-25", "--request-group", "A+", "--units", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["allocation"]["allocated_units"][0]["unit_id"] == "A1"

    # Audit catches expired bag
    health = audit_inventory_health(
        "H1",
        [BloodUnit("EXP1", "H1", BloodGroup.O_POS, BloodComponent.PRBC, date(2026, 11, 20))],
        as_of=date(2026, 11, 25),
    )
    assert health.expired_units_to_quarantine == 1
    assert health.o_neg_below_reserve is True
