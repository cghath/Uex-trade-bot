"""Saved trading-preference display and defaults - dependency-free, no I/O.

Storage and route-command wiring live in bot/db/database.py and bot/cogs/*.py; this module
only knows how to describe a resolved set of preferences back to the user.
"""
from __future__ import annotations

from collections.abc import Collection

from bot.uex.commodity_risk import RISK_TOLERANCE_SKIPS

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

# Every command that suggests routes, and which of them read each saved default when its
# own matching option is left unset (audit MSG-6: the descriptions had drifted from the
# code). The option descriptions and /my-trading-preferences are worded from this, and
# tests/test_preference_scope.py checks it against each command's code.
ROUTE_COMMANDS = (
    "best-route", "top-routes", "mixed-routes", "multi-stop-route", "diminishing-returns", "intelligence-brief",
)
_MIXED_CARGO_COMMANDS = ("mixed-routes", "multi-stop-route", "diminishing-returns", "intelligence-brief")
PREFERENCE_READERS: dict[str, tuple[str, ...]] = {
    # /best-route has no budget option; /diminishing-returns sweeps budgets itself.
    "budget": tuple(c for c in ROUTE_COMMANDS if c not in ("best-route", "diminishing-returns")),
    # Only the mixed-cargo commands know each terminal's pads and whether it's a station.
    "space_only": _MIXED_CARGO_COMMANDS,
    "capital_ship_access": _MIXED_CARGO_COMMANDS,
    "auto_load_only": ROUTE_COMMANDS,
    # /top-routes skips it when both its ends are named: nothing is left to restrict.
    "preferred_system": ROUTE_COMMANDS,
}


def preference_scope(key: str) -> str:
    """Which commands a saved default applies to, e.g. "every route command except
    /best-route and /diminishing-returns" - from PREFERENCE_READERS, so the wording can't
    drift from it."""
    readers = PREFERENCE_READERS[key]
    left_out = [command for command in ROUTE_COMMANDS if command not in readers]
    if not left_out:
        return "every route command"
    if len(left_out) <= 2:
        return "every route command except " + " and ".join(f"/{command}" for command in left_out)
    return ", ".join(f"/{command}" for command in readers)


def describe_active_preferences(
    *,
    space_only: bool = False,
    capital_ship_access: bool = False,
    auto_load_only: bool = False,
    system: str | None = None,
    risk_tolerance: str | None = None,
    saved: Collection[str] = (),
) -> str | None:
    """The footer line naming every filter shaping this result, or None if none are on.
    Every route command shows it (audit MSG-8), so a saved default never applies unseen.

    Only pass the fields the calling command applies - a command with no space-only or
    capital-ship-access concept (e.g. /best-route) should simply not pass them. `saved`
    names the keyword arguments that came from the player's saved /set-trading-preferences
    rather than this command's own options; those are marked "(saved)". Capital-ship
    access and risk tolerance have no per-command option, so they're always saved: pass
    capital_ship_access only for the saved preference, not for a ship that needs it."""
    saved = {*saved, "capital_ship_access", "risk_tolerance"}
    parts: list[tuple[str, str]] = []
    if space_only:
        parts.append(("space_only", "space-only"))
    if capital_ship_access:
        parts.append(("capital_ship_access", "capital-ship access"))
    if auto_load_only:
        parts.append(("auto_load_only", "auto-load-only"))
    if system:
        parts.append(("system", f"system: {system}"))
    if risk_tolerance and risk_tolerance != "high":
        parts.append(("risk_tolerance", f"risk tolerance: {risk_tolerance}"))
    if not parts:
        return None
    return "Filters: " + ", ".join(f"{label} (saved)" if key in saved else label for key, label in parts)


def format_trading_preferences(prefs: dict[str, object], *, ship_detail: str | None = None) -> str:
    """Full current-state listing for /my-trading-preferences and /set-trading-preferences'
    confirmation - every field, not just the active ones describe_active_preferences shows.

    ship_detail is an optional, already-resolved string appended to the ship line (e.g. a
    live cargo-capacity figure, or a "couldn't be matched" staleness note) - this module
    stays dependency-free/no-I/O per its own docstring, so any live UEX lookup happens in
    the caller (bot/cogs/trading_preferences.py's /my-trading-preferences, mirroring the
    former /my-ship's own lookup) and is only ever passed in as plain text, never fetched
    here."""
    def _yes_no(value: object) -> str:
        return "Yes" if value else "No"

    system = prefs.get("preferred_system") or "Any (no restriction)"
    risk = prefs.get("risk_tolerance") or "High (no restriction, default)"
    ship = prefs.get("ship_name") or "None set"
    ship_line = f"Default ship: **{ship}**" + (f" {ship_detail}" if ship_detail else "")
    budget = prefs.get("budget")
    budget_display = f"{budget:,.0f} aUEC" if budget else "None set"
    return "\n".join([
        ship_line,
        f"Default budget: **{budget_display}** ({preference_scope('budget')})",
        f"Space-only terminals: **{_yes_no(prefs.get('space_only'))}** ({preference_scope('space_only')})",
        f"Capital-ship access required: **{_yes_no(prefs.get('capital_ship_access'))}** "
        f"({preference_scope('capital_ship_access')})",
        f"Auto-load only: **{_yes_no(prefs.get('auto_load_only'))}** ({preference_scope('auto_load_only')})",
        f"Preferred system: **{system}** ({preference_scope('preferred_system')})",
        f"Risk tolerance: **{risk}**" + (f" (route suggestions skip {RISK_TOLERANCE_SKIPS[risk]})"
                                          if risk in RISK_TOLERANCE_SKIPS else ""),
    ])


def risk_tolerance_hint(tolerance: str | None) -> str:
    """Appended to a "nothing found" message when the saved risk tolerance left routes out.
    It has no per-command option to override, unlike saved_filters_hint's filters."""
    if tolerance not in RISK_TOLERANCE_SKIPS:
        return ""
    return (f" Your saved risk tolerance ({tolerance}) skips {RISK_TOLERANCE_SKIPS[tolerance]} - "
            "change it with /set-trading-preferences.")


def saved_filters_hint(labels: list[str], *, can_override: bool = True) -> str:
    """Appended to a "nothing found" message when filters the player didn't pass on this
    command - their saved /set-trading-preferences defaults - are active (audit UX-2).
    Without it a saved auto-load-only or system filter that rules out everything reads as
    "nothing exists right now", with no hint the player's own setting is the cause.
    `labels` are the saved filters actually active, e.g. ["auto-load-only", "system Pyro"];
    empty means no hint. `can_override=False` is for a command without options for them."""
    if not labels:
        return ""
    if len(labels) == 1:
        override = "set that option on this command to override it, or " if can_override else ""
        return f" Your saved {labels[0]} setting is on - {override}change it with /set-trading-preferences."
    joined = ", ".join(labels[:-1]) + " and " + labels[-1]
    override = "set those options on this command to override them, or " if can_override else ""
    return f" Your saved {joined} settings are on - {override}change them with /set-trading-preferences."


def saved_filter_labels(*, space_only: bool = False, capital_ship_access: bool = False,
                        auto_load_only: bool = False, system: str | None = None) -> list[str]:
    """Labels for saved_filters_hint. Pass only filters that came from saved preferences,
    not ones the player set on this command."""
    labels = []
    if space_only:
        labels.append("space-only")
    if capital_ship_access:
        labels.append("capital-ship access")
    if auto_load_only:
        labels.append("auto-load-only")
    if system:
        labels.append(f"system {system}")
    return labels
