"""
Smart Holiday Delivery Scheduler — flags shipments when public holidays overlap
origin, transit hubs, or destination during the planned corridor window.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

import holidays

# ---------------------------------------------------------------------------
# Route model: each leg is (country ISO code, first_day_offset, end_day_offset).
# Day offsets are half-open [start, end) relative to the ship date (day 0).
# Adjust legs to match your carrier’s typical lead times.
# ---------------------------------------------------------------------------

ROUTES: dict[tuple[str, str], dict] = {
    ("LT", "DE"): {
        "label": "Lithuania -> Germany (via Poland)",
        "transit": ["PL"],
        "legs": [
            ("LT", 0, 2),
            ("PL", 2, 6),
            ("DE", 6, 9),
        ],
    },
    ("LT", "PL"): {
        "label": "Lithuania -> Poland (direct)",
        "transit": [],
        "legs": [
            ("LT", 0, 2),
            ("PL", 2, 5),
        ],
    },
    ("DE", "FR"): {
        "label": "Germany -> France (via Belgium hub)",
        "transit": ["BE"],
        "legs": [
            ("DE", 0, 2),
            ("BE", 2, 4),
            ("FR", 4, 7),
        ],
    },
}


@dataclass
class DayConflict:
    """A calendar day where the active corridor country has a public holiday."""

    calendar_day: date
    day_offset: int
    country: str
    holiday_name: str


@dataclass
class ScheduleReport:
    """Result of checking one proposed ship date."""

    origin: str
    destination: str
    ship_date: date
    route_label: str
    transit_countries: list[str]
    conflicts: list[DayConflict] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return len(self.conflicts) == 0


def _years_touching(start: date, days: int) -> list[int]:
    ys: set[int] = set()
    for i in range(days):
        ys.add((start + timedelta(days=i)).year)
    return sorted(ys)


def _holiday_calendar(country: str, years: Iterable[int]) -> holidays.HolidayBase:
    year_list = sorted(set(years))
    return holidays.country_holidays(country, years=year_list)


def _country_for_offset(
    legs: list[tuple[str, int, int]], offset: int
) -> str | None:
    for code, start, end in legs:
        if start <= offset < end:
            return code
    return None


def _max_leg_end(legs: list[tuple[str, int, int]]) -> int:
    return max(end for _, _, end in legs)


def _holiday_name(cal: holidays.HolidayBase, d: date) -> str:
    name = cal.get(d)
    if name is None:
        return "Public holiday"
    if isinstance(name, list):
        return name[0] if name else "Public holiday"
    return str(name)


def evaluate_ship_date(
    origin: str,
    destination: str,
    ship_date: date,
) -> ScheduleReport:
    key = (origin.upper(), destination.upper())
    if key not in ROUTES:
        raise KeyError(f"No route defined for {origin} -> {destination}")

    cfg = ROUTES[key]
    legs = cfg["legs"]
    span = _max_leg_end(legs)
    years = _years_touching(ship_date, span)
    calendars: dict[str, holidays.HolidayBase] = {}

    def cal_for(code: str) -> holidays.HolidayBase:
        if code not in calendars:
            calendars[code] = _holiday_calendar(code, years)
        return calendars[code]

    conflicts: list[DayConflict] = []
    for offset in range(span):
        d = ship_date + timedelta(days=offset)
        country = _country_for_offset(legs, offset)
        if country is None:
            continue
        cal = cal_for(country)
        if d in cal:
            conflicts.append(
                DayConflict(
                    calendar_day=d,
                    day_offset=offset,
                    country=country,
                    holiday_name=_holiday_name(cal, d),
                )
            )

    return ScheduleReport(
        origin=key[0],
        destination=key[1],
        ship_date=ship_date,
        route_label=cfg["label"],
        transit_countries=list(cfg.get("transit", [])),
        conflicts=conflicts,
    )


def suggest_safe_ship_date(
    origin: str,
    destination: str,
    start_from: date,
    *,
    max_ahead_days: int = 120,
) -> date | None:
    """
    Return the earliest date on or after start_from with no holiday conflicts
    along the route window, or None if none found within the search horizon.
    """
    for delta in range(max_ahead_days + 1):
        candidate = start_from + timedelta(days=delta)
        report = evaluate_ship_date(origin, destination, candidate)
        if report.is_safe:
            return candidate
    return None


def _format_report_text(report: ScheduleReport, suggestion: date | None) -> str:
    lines: list[str] = []
    lines.append(f"Route: {report.route_label} ({report.origin} -> {report.destination})")
    if report.transit_countries:
        lines.append(f"Transit hubs in model: {', '.join(report.transit_countries)}")
    lines.append(f"Proposed ship date: {report.ship_date.isoformat()}")
    lines.append("")

    if report.is_safe:
        lines.append("Status: OK — no public holidays along the modeled corridor window.")
    else:
        lines.append("Status: FLAG — holiday overlap in an active corridor country.")
        lines.append("")
        for c in report.conflicts:
            lines.append(
                f"  • {c.calendar_day.isoformat()} (day +{c.day_offset}) — "
                f"{c.country}: {c.holiday_name}"
            )
        lines.append("")
        if suggestion is not None:
            if suggestion == report.ship_date:
                lines.append("Suggested safe ship date: (same day — fix model if unexpected)")
            else:
                lines.append(f"Suggested safe ship date: {suggestion.isoformat()}")
        else:
            lines.append("No safe date found within the search window; widen legs or horizon.")

    return "\n".join(lines)


def _parse_iso_date(s: str) -> date:
    return date.fromisoformat(s)


def _configure_stdout_utf8() -> None:
    """Avoid Windows console UnicodeEncodeError for holiday names (e.g. Polish)."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> None:
    _configure_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="Check a ship date against public holidays on origin, transit, and destination."
    )
    parser.add_argument("--origin", "-o", help="Origin country (ISO 3166-1 alpha-2), e.g. LT")
    parser.add_argument("--destination", "-d", help="Destination country, e.g. DE")
    parser.add_argument("--date", "-t", help="Planned ship date (YYYY-MM-DD)")
    parser.add_argument(
        "--list-routes",
        action="store_true",
        help="Print configured routes and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )
    args = parser.parse_args()

    if args.list_routes:
        for (a, b), cfg in ROUTES.items():
            print(f"{a} -> {b}: {cfg['label']}")
        return

    if not args.origin or not args.destination or not args.date:
        parser.error("--origin, --destination, and --date are required (or use --list-routes)")

    ship = _parse_iso_date(args.date)
    report = evaluate_ship_date(args.origin, args.destination, ship)
    suggestion = suggest_safe_ship_date(args.origin, args.destination, ship)

    if args.json:
        payload = {
            "origin": report.origin,
            "destination": report.destination,
            "ship_date": report.ship_date.isoformat(),
            "route_label": report.route_label,
            "transit_countries": report.transit_countries,
            "is_safe": report.is_safe,
            "conflicts": [
                {
                    "date": c.calendar_day.isoformat(),
                    "day_offset": c.day_offset,
                    "country": c.country,
                    "holiday": c.holiday_name,
                }
                for c in report.conflicts
            ],
            "suggested_safe_ship_date": suggestion.isoformat() if suggestion else None,
        }
        print(json.dumps(payload, indent=2))
        return

    print(_format_report_text(report, suggestion))


if __name__ == "__main__":
    main()
