"""Saved trading-preference display and defaults - dependency-free, no I/O.

Storage and route-command wiring live in bot/db/database.py and bot/cogs/*.py; this module
only knows how to describe a resolved set of preferences back to the user.
"""
from __future__ import annotations

# Sentinel for Database.set_trading_preferences: distinguishes "caller didn't pass this
# field, leave it unchanged" from a real value (including False/None, both meaningful).
# Public (no leading underscore) because callers outside bot/db/database.py - the
# trading-preferences cog included - need to pass it explicitly for fields they aren't
# changing in a given /set-trading-preferences call.
UNSET = object()

DEFAULT_TRADING_PREFERENCES: dict[str, object] = {
    "space_only": False,
    "capital_ship_access": False,
    "auto_load_only": False,
    "preferred_system": None,
    "risk_tolerance": None,
    "ship_name": None,
    "budget": None,
}

# Ordered low -> high; "high" means no restriction (the default once a filter exists).
RISK_TOLERANCE_LEVELS = ("low", "medium", "high")


def describe_active_preferences(
    *,
    space_only: bool = False,
    capital_ship_access: bool = False,
    auto_load_only: bool = False,
    system: str | None = None,
    risk_tolerance: str | None = None,
) -> str | None:
    """One short line naming which preferences are shaping this result, or None if none
    are active. Only pass the fields relevant to the calling command - a command with no
    space-only/capital-ship-access concept (e.g. /best-route) should simply not pass them,
    rather than have this function guess which fields apply."""
    parts: list[str] = []
    if space_only:
        parts.append("space-only")
    if capital_ship_access:
        parts.append("capital-ship access")
    if auto_load_only:
        parts.append("auto-load-only")
    if system:
        parts.append(f"system: {system}")
    if risk_tolerance and risk_tolerance != "high":
        parts.append(f"risk tolerance: {risk_tolerance} (not yet enforced)")
    if not parts:
        return None
    return "Active preferences: " + ", ".join(parts)


def format_trading_preferences(prefs: dict[str, object]) -> str:
    """Full current-state listing for /my-trading-preferences and /set-trading-preferences'
    confirmation - every field, not just the active ones describe_active_preferences shows."""
    def _yes_no(value: object) -> str:
        return "Yes" if value else "No"

    system = prefs.get("preferred_system") or "Any (no restriction)"
    risk = prefs.get("risk_tolerance") or "High (no restriction, default)"
    ship = prefs.get("ship_name") or "None set"
    budget = prefs.get("budget")
    budget_display = f"{budget:,.0f} aUEC" if budget else "None set"
    return "\n".join([
        f"Default ship: **{ship}**",
        f"Default budget: **{budget_display}** (mixed-routes/multi-stop-route/route-from-multi/route-on-the-way only)",
        f"Space-only terminals: **{_yes_no(prefs.get('space_only'))}** (mixed-routes/multi-stop-route only)",
        f"Capital-ship access required: **{_yes_no(prefs.get('capital_ship_access'))}** (mixed-routes/multi-stop-route only)",
        f"Auto-load only: **{_yes_no(prefs.get('auto_load_only'))}**",
        f"Preferred system: **{system}**",
        f"Risk tolerance: **{risk}** (stored, not yet enforced by any route command)",
    ])
