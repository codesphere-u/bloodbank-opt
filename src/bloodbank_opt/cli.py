"""CLI entrypoint for bloodbank-opt."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from .inventory import (
    BloodComponent,
    BloodGroup,
    BloodUnit,
    allocate_prbc_request,
    audit_inventory_health,
)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bloodbank-opt",
        description="Audit blood-bank expiry health and allocate PRBC units with O-negative conservation.",
    )
    parser.add_argument("inventory_json", type=Path, help="Path to blood-bank inventory JSON.")
    parser.add_argument("--facility", required=True, help="Facility ID to audit.")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Audit date YYYY-MM-DD.")
    parser.add_argument("--request-group", default=None, help="Optional recipient ABO/RhD group to allocate.")
    parser.add_argument("--units", type=int, default=2, help="Units requested if --request-group is provided.")
    args = parser.parse_args(argv)

    as_of = date.fromisoformat(args.as_of)
    raw = json.loads(args.inventory_json.read_text(encoding="utf-8"))
    units = [
        BloodUnit(
            unit_id=str(u["unit_id"]),
            facility_id=str(u["facility_id"]),
            blood_group=BloodGroup(str(u["blood_group"])),
            component=BloodComponent(str(u.get("component", "prbc"))),
            expiry_date=date.fromisoformat(str(u["expiry_date"])),
            volume_ml=int(u.get("volume_ml", 300)),
        )
        for u in raw["units"]
    ]
    health = audit_inventory_health(args.facility, units, as_of=as_of)
    out: dict[str, Any] = {"inventory_health": asdict(health)}
    if args.request_group:
        fac_units = [u for u in units if u.facility_id == args.facility]
        alloc = allocate_prbc_request(
            fac_units,
            recipient_group=BloodGroup(args.request_group),
            units_requested=args.units,
            as_of=as_of,
        )
        out["allocation"] = asdict(alloc)
    print(json.dumps(out, indent=2, default=_json_default))
    return 0 if not health.o_neg_below_reserve and health.expired_units_to_quarantine == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
