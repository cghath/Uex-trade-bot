"""Operational route notes from durable terminal-reference metadata."""
from __future__ import annotations

from typing import Any


def terminal_practical_notes(label: str, terminal: dict[str, Any] | None) -> list[str]:
    """Return concise confirmed limitations and useful on-site services."""
    if not terminal:
        return []
    notes: list[str] = []
    max_container = int(terminal.get("max_container_size") or 0)
    if max_container and max_container < 32:
        notes.append(f"⚠️ {label}: maximum container size {max_container} SCU")
    if not terminal.get("has_freight_elevator") and not terminal.get("has_loading_dock"):
        notes.append(f"⚠️ {label}: no freight elevator or loading dock reported")
    if terminal.get("is_player_owned"):
        notes.append(f"⚠️ {label}: player-owned location; access and availability may change")

    services = []
    if terminal.get("is_refuel"):
        services.append("refuel")
    if terminal.get("is_repair"):
        services.append("repair")
    if terminal.get("is_cargo_center"):
        services.append("cargo center")
    if services:
        notes.append(f"{label} services: {', '.join(services)}")
    return notes


def route_practical_notes(
    origin: dict[str, Any] | None, destination: dict[str, Any] | None
) -> list[str]:
    return [
        *terminal_practical_notes("Origin", origin),
        *terminal_practical_notes("Destination", destination),
    ]


def terminal_supports_auto_load(terminal: dict[str, Any] | None) -> bool:
    """True only when UEX explicitly reports auto-load at this terminal.

    This is UEX's `is_auto_load` field - a terminal-level flag, not one scoped to buy or
    sell specifically - which is a different field than `has_loading_dock`/
    `has_freight_elevator` (physical external cargo infrastructure, used by practical
    notes and mixed-route capital-ship gating above). Missing/unknown terminals fail
    closed, matching the other route safety filters in this codebase.
    """
    return bool(terminal) and bool(terminal.get("is_auto_load"))


def route_supports_auto_load(
    origin: dict[str, Any] | None, destination: dict[str, Any] | None
) -> bool:
    """True only when BOTH ends of a route are confirmed to offer auto-load - per
    user direction, not just the origin (buy) side an earlier pass had assumed, since
    `is_auto_load` is a property of the terminal itself and isn't documented as specific
    to either direction.
    """
    return terminal_supports_auto_load(origin) and terminal_supports_auto_load(destination)


def terminal_in_system(terminal: dict[str, Any] | None, system: str) -> bool:
    """True only when UEX explicitly places this terminal in the named star system
    (confirmed live values: 'Stanton', 'Pyro', 'Nyx'). Missing/unknown terminals fail
    closed, matching the other route safety filters in this codebase.
    """
    return bool(terminal) and terminal.get("star_system_name") == system


def route_in_system(
    origin: dict[str, Any] | None, destination: dict[str, Any] | None, system: str | None
) -> bool:
    """True when no system filter is requested, or both ends of a route are confirmed
    to be in the requested system - a route that crosses systems doesn't satisfy "stay
    in Pyro" just because one end happens to be there.
    """
    if system is None:
        return True
    return terminal_in_system(origin, system) and terminal_in_system(destination, system)
