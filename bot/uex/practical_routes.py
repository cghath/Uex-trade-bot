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
