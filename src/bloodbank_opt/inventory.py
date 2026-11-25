"""Blood-bank ABO/RhD compatibility, O-negative conservation FEFO allocator & hub transfers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class BloodGroup(StrEnum):
    O_NEG = "O-"
    O_POS = "O+"
    A_NEG = "A-"
    A_POS = "A+"
    B_NEG = "B-"
    B_POS = "B+"
    AB_NEG = "AB-"
    AB_POS = "AB+"


class BloodComponent(StrEnum):
    PRBC = "prbc"  # Packed Red Blood Cells (35d CPDA-1 / 42d SAGM)
    WHOLE_BLOOD = "whole_blood"  # 35d
    PLATELETS = "platelets"  # 5d at 20-24C agitation
    FFP = "ffp"  # Fresh Frozen Plasma (365d at <= -18C)


# Ordered substitution preference for PRBC recipients (exact match first; O- strictly last)
_RBC_DONOR_PRIORITY: dict[BloodGroup, tuple[BloodGroup, ...]] = {
    BloodGroup.O_NEG: (BloodGroup.O_NEG,),
    BloodGroup.O_POS: (BloodGroup.O_POS, BloodGroup.O_NEG),
    BloodGroup.A_NEG: (BloodGroup.A_NEG, BloodGroup.O_NEG),
    BloodGroup.A_POS: (BloodGroup.A_POS, BloodGroup.A_NEG, BloodGroup.O_POS, BloodGroup.O_NEG),
    BloodGroup.B_NEG: (BloodGroup.B_NEG, BloodGroup.O_NEG),
    BloodGroup.B_POS: (BloodGroup.B_POS, BloodGroup.B_NEG, BloodGroup.O_POS, BloodGroup.O_NEG),
    BloodGroup.AB_NEG: (BloodGroup.AB_NEG, BloodGroup.A_NEG, BloodGroup.B_NEG, BloodGroup.O_NEG),
    BloodGroup.AB_POS: (
        BloodGroup.AB_POS,
        BloodGroup.AB_NEG,
        BloodGroup.A_POS,
        BloodGroup.B_POS,
        BloodGroup.A_NEG,
        BloodGroup.B_NEG,
        BloodGroup.O_POS,
        BloodGroup.O_NEG,
    ),
}


def _abo(group: BloodGroup) -> str:
    return group.value[:-1]


def compatible_rbc_donors(recipient: BloodGroup) -> tuple[BloodGroup, ...]:
    """Return compatible PRBC donor groups ordered from most preferred to universal O-."""
    return _RBC_DONOR_PRIORITY[recipient]


def is_rbc_compatible(donor: BloodGroup, recipient: BloodGroup) -> bool:
    """Return True if donor Packed Red Blood Cells can safely transfuse recipient."""
    return donor in _RBC_DONOR_PRIORITY[recipient]


def compatible_plasma_donors(recipient: BloodGroup) -> tuple[BloodGroup, ...]:
    """Return ABO-compatible Fresh Frozen Plasma (FFP) donor groups (AB is universal plasma donor)."""
    r_abo = _abo(recipient)
    allowed_abos: set[str]
    if r_abo == "AB":
        allowed_abos = {"AB"}
    elif r_abo == "A":
        allowed_abos = {"A", "AB"}
    elif r_abo == "B":
        allowed_abos = {"B", "AB"}
    else:
        allowed_abos = {"O", "A", "B", "AB"}
    return tuple(g for g in BloodGroup if _abo(g) in allowed_abos)


def is_plasma_compatible(donor: BloodGroup, recipient: BloodGroup) -> bool:
    """Return True if donor FFP plasma is ABO-compatible with recipient."""
    return donor in compatible_plasma_donors(recipient)


@dataclass(frozen=True)
class BloodUnit:
    """Individual tracked blood component bag in a facility blood bank."""

    unit_id: str
    facility_id: str
    blood_group: BloodGroup
    component: BloodComponent
    expiry_date: date
    volume_ml: int = 300

    def days_until_expiry(self, as_of: date) -> int:
        return (self.expiry_date - as_of).days


@dataclass(frozen=True)
class AllocationDecision:
    """FEFO + O-negative conservation transfusion allocation result."""

    recipient_group: BloodGroup
    units_requested: int
    allocated_units: tuple[BloodUnit, ...]
    shortfall_units: int
    used_substitution: bool
    o_neg_reserve_protected: bool
    notes: tuple[str, ...]


def allocate_prbc_request(
    inventory: Sequence[BloodUnit],
    *,
    recipient_group: BloodGroup,
    units_requested: int,
    as_of: date,
    o_neg_emergency_reserve: int = 4,
    emergency_override: bool = False,
    near_expiry_rescue_days: int = 3,
) -> AllocationDecision:
    """Allocate unexpired PRBC/Whole Blood units via FEFO while conserving O-negative reserve."""
    if units_requested <= 0:
        raise ValueError("units_requested must be positive")

    eligible = [
        u
        for u in inventory
        if u.component in (BloodComponent.PRBC, BloodComponent.WHOLE_BLOOD)
        and u.days_until_expiry(as_of) >= 0
        and is_rbc_compatible(u.blood_group, recipient_group)
    ]

    o_neg_total = sum(
        1
        for u in inventory
        if u.component in (BloodComponent.PRBC, BloodComponent.WHOLE_BLOOD)
        and u.days_until_expiry(as_of) >= 0
        and u.blood_group == BloodGroup.O_NEG
    )

    priority_list = _RBC_DONOR_PRIORITY[recipient_group]
    rank_map = {grp: idx for idx, grp in enumerate(priority_list)}

    def _sort_key(unit: BloodUnit) -> tuple[int, int, int, str]:
        dte = unit.days_until_expiry(as_of)
        # Near-expiry rescue for non-O-neg compatible bags (<=3 days left) prevents preventable discard
        near_exp_boost = 0 if (unit.blood_group != BloodGroup.O_NEG and dte <= near_expiry_rescue_days) else 1
        return (near_exp_boost, rank_map[unit.blood_group], dte, unit.unit_id)

    eligible.sort(key=_sort_key)

    selected: list[BloodUnit] = []
    notes: list[str] = []
    o_neg_remaining = o_neg_total
    reserve_protected = False

    for unit in eligible:
        if len(selected) >= units_requested:
            break
        is_sub_o_neg = unit.blood_group == BloodGroup.O_NEG and recipient_group != BloodGroup.O_NEG
        if is_sub_o_neg and not emergency_override and o_neg_remaining <= o_neg_emergency_reserve:
            reserve_protected = True
            continue
        selected.append(unit)
        if unit.blood_group == BloodGroup.O_NEG:
            o_neg_remaining -= 1

    used_sub = any(u.blood_group != recipient_group for u in selected)
    shortfall = units_requested - len(selected)
    if reserve_protected and shortfall > 0:
        notes.append(
            f"O-NEG RESERVE PROTECTED: held {o_neg_emergency_reserve} O- emergency units for unknown-type "
            "hemorrhage/trauma (pass emergency_override=True if life-threatening)."
        )
    if used_sub:
        groups_used = [u.blood_group.value for u in selected]
        notes.append(f"Compatible non-identical substitution used: {groups_used} -> {recipient_group.value}.")
    if shortfall > 0:
        notes.append(
            f"SHORTFALL: {shortfall} unit(s) unfulfilled for {recipient_group.value}; request regional hub transfer."
        )

    return AllocationDecision(
        recipient_group=recipient_group,
        units_requested=units_requested,
        allocated_units=tuple(selected),
        shortfall_units=shortfall,
        used_substitution=used_sub,
        o_neg_reserve_protected=reserve_protected,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class FacilityStockSummary:
    """Blood-bank expiry and emergency-reserve health report."""

    facility_id: str
    as_of: str
    total_active_units: int
    expired_units_to_quarantine: int
    near_expiry_units_le_7d: int
    counts_by_group: dict[str, int]
    o_neg_below_reserve: bool
    alerts: tuple[str, ...]


def audit_inventory_health(
    facility_id: str,
    inventory: Sequence[BloodUnit],
    *,
    as_of: date,
    o_neg_reserve_min: int = 4,
    near_expiry_days: int = 7,
) -> FacilityStockSummary:
    """Audit expired stock, near-expiry waste risk, and O-negative reserve sufficiency."""
    expired = 0
    near_exp = 0
    active = 0
    counts: dict[str, int] = {g.value: 0 for g in BloodGroup}

    for u in inventory:
        if u.facility_id != facility_id:
            continue
        dte = u.days_until_expiry(as_of)
        if dte < 0:
            expired += 1
        else:
            active += 1
            counts[u.blood_group.value] += 1
            if dte <= near_expiry_days:
                near_exp += 1

    o_neg_low = counts[BloodGroup.O_NEG.value] < o_neg_reserve_min
    alerts: list[str] = []
    if expired > 0:
        alerts.append(f"QUARANTINE IMMEDIATELY: {expired} expired unit(s) still listed in {facility_id} inventory.")
    if o_neg_low:
        alerts.append(
            f"CRITICAL O-NEG SHORTAGE: {counts['O-']} O- unit(s) on hand (< {o_neg_reserve_min} emergency reserve)."
        )
    if near_exp > 0:
        alerts.append(
            f"EXPIRY RISK: {near_exp} unit(s) expire within {near_expiry_days} days; prioritize hub transfer."
        )

    return FacilityStockSummary(
        facility_id=facility_id,
        as_of=as_of.isoformat(),
        total_active_units=active,
        expired_units_to_quarantine=expired,
        near_expiry_units_le_7d=near_exp,
        counts_by_group=counts,
        o_neg_below_reserve=o_neg_low,
        alerts=tuple(alerts),
    )


@dataclass(frozen=True)
class HubTransferRecommendation:
    """Recommended inter-facility redistribution of near-expiry compatible blood units."""

    from_facility: str
    to_facility: str
    unit_id: str
    blood_group: BloodGroup
    days_until_expiry: int


def match_regional_transfers(
    inventory: Sequence[BloodUnit],
    facility_deficits: dict[str, dict[BloodGroup, int]],
    *,
    as_of: date,
    surplus_expiry_window_days: int = 7,
) -> list[HubTransferRecommendation]:
    """Match near-expiry PRBC units at low-demand clinics to compatible regional hospital deficits."""
    needed = {fac: dict(grps) for fac, grps in facility_deficits.items()}
    candidates = [
        u
        for u in inventory
        if u.component in (BloodComponent.PRBC, BloodComponent.WHOLE_BLOOD)
        and 1 <= u.days_until_expiry(as_of) <= surplus_expiry_window_days
    ]
    candidates.sort(key=lambda u: (u.days_until_expiry(as_of), u.unit_id))

    transfers: list[HubTransferRecommendation] = []
    for unit in candidates:
        # Don't pull a unit from a facility that itself has a deficit for that exact group
        if needed.get(unit.facility_id, {}).get(unit.blood_group, 0) > 0:
            continue
        matched = False
        # Prefer exact group match at destination hospital first
        for dest_fac, grps in needed.items():
            if dest_fac == unit.facility_id:
                continue
            if grps.get(unit.blood_group, 0) > 0:
                grps[unit.blood_group] -= 1
                transfers.append(
                    HubTransferRecommendation(
                        from_facility=unit.facility_id,
                        to_facility=dest_fac,
                        unit_id=unit.unit_id,
                        blood_group=unit.blood_group,
                        days_until_expiry=unit.days_until_expiry(as_of),
                    )
                )
                matched = True
                break
        if matched:
            continue
        # Fallback: compatible non-O-neg substitution at destination hospital
        if unit.blood_group != BloodGroup.O_NEG:
            for dest_fac, grps in needed.items():
                if dest_fac == unit.facility_id:
                    continue
                for rec_grp, qty in grps.items():
                    if qty > 0 and is_rbc_compatible(unit.blood_group, rec_grp):
                        grps[rec_grp] -= 1
                        transfers.append(
                            HubTransferRecommendation(
                                from_facility=unit.facility_id,
                                to_facility=dest_fac,
                                unit_id=unit.unit_id,
                                blood_group=unit.blood_group,
                                days_until_expiry=unit.days_until_expiry(as_of),
                            )
                        )
                        matched = True
                        break
                if matched:
                    break
    return transfers
