"""Diagnostic commands: /test-dm and /command-usage.

Motivated by a real, recurring problem with Discord bots in general: whether a bot's DM
actually reaches a given user depends on THEIR Discord settings (a per-server "Allow direct
messages from server members" toggle, or having blocked the bot outright), not anything the
bot does - and the failure looks identical from the bot's side either way (Discord just
returns a 403 Forbidden). Every alert DMs by default (/alert-add, /stock-alert-add,
/marketplace-alert-add); a refused DM now falls back to the alert's channel (bot/delivery.py),
but a player still wants to know their DMs work. /test-dm checks that *before* depending on
it, rather than discovering it during a real alert.

/command-usage is owner-only: a running count of real command usage (bot.uex.main's
on_app_command_completion listener feeds command_usage_by_user), specifically to inform
trimming the command surface for new-user friendliness - see CONTRIBUTING.md/
PROJECT_CONTEXT.md. The owner's own constant testing is tracked but left out: the report
shows only other players' usage (usage_report). Its optional `command` option drills into
who (by display name, not just a count) has actually run one specific command, for
reaching out to real users for feedback.
"""
from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands


async def tracked_command_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Suggests only commands with at least one recorded invocation - a name from the live
    command tree with zero usage would just send the owner to an empty drill-down."""
    try:
        owner_ids = interaction.client.owner_ids or (
            {interaction.client.owner_id} if interaction.client.owner_id else set()
        )
        stats = await interaction.client.db.get_command_usage_stats(owner_ids)
    except Exception:
        return []
    current_lower = current.lower()
    matches = [row["command_name"] for row in stats if current_lower in row["command_name"].lower()][:25]
    return [app_commands.Choice(name=name, value=name) for name in matches]


# Discord's plain-message cap is 2,000 characters; the report stops adding commands short of
# it and says how many more there are.
REPORT_LIMIT = 1900
# Players named on one command's line before "+N more".
NAMES_SHOWN = 4


def _day(timestamp: str | None) -> str:
    """'2026-10-04 16:03:33' -> 'Oct 4'."""
    try:
        day = datetime.strptime((timestamp or "")[:10], "%Y-%m-%d")
    except ValueError:
        return "unknown"
    return f"{day:%b} {day.day}"


def _player_name(name: str) -> str:
    """A display name is the player's own text: no mention can ping and no markdown can break
    the line. Isolated (U+2068/U+2069) so a right-to-left name doesn't reorder what follows."""
    return "\u2068" + discord.utils.escape_markdown(discord.utils.escape_mentions(name)) + "\u2069"


def usage_report(rows: list[dict], live_names: set[str]) -> str:
    """The /command-usage report: other players' usage only (`rows` already leaves the owner
    out - Database.get_real_command_usage), most-used command first, each with who ran it.
    Commands no other player has run are one count, not a list; a retired command's usage is
    one small line, kept for history (audit: it used to pollute the live ranking)."""
    names: dict[int, str] = {}
    for row in sorted(rows, key=lambda r: r["last_used_at"] or ""):
        if row["username"]:
            names[row["user_id"]] = row["username"]  # the latest name the player went by
    live: dict[str, list[dict]] = {}
    retired: dict[str, int] = {}
    for row in rows:
        if row["command_name"] in live_names:
            live.setdefault(row["command_name"], []).append(row)
        else:
            retired[row["command_name"]] = retired.get(row["command_name"], 0) + row["use_count"]

    lines = ["## Real usage", "-# Other players only - your own testing is left out"]
    if live:
        players = len({row["user_id"] for users in live.values() for row in users})
        uses = sum(row["use_count"] for users in live.values() for row in users)
        lines.append(f"**{players} player{'s' if players != 1 else ''}** · **{uses} use{'s' if uses != 1 else ''}** · "
                     f"{len(live)} of {len(live_names)} commands")
    else:
        lines.append("No other player has run a command yet.")
    footer = []
    unused = len(live_names) - len(live)
    if unused and live:
        footer.append(f"-# {unused} command{'s' if unused != 1 else ''} no other player has run yet")
    if retired:
        footer.append("-# Retired, kept for history: " + ", ".join(
            f"/{name} ({count} use{'s' if count != 1 else ''})" for name, count in sorted(retired.items())))
    if live:
        footer.append("-# Add `command:` to see who ran one, with a link to each player")

    # Most uses first; a tie goes to the command used most recently (stable sorts, last key first).
    ranked = sorted(live.items(), key=lambda item: max(r["last_used_at"] or "" for r in item[1]), reverse=True)
    ranked.sort(key=lambda item: -sum(r["use_count"] for r in item[1]))
    blocks = []
    for name, users in ranked:
        users = sorted(users, key=lambda r: r["last_used_at"] or "", reverse=True)
        users.sort(key=lambda r: -r["use_count"])
        uses = sum(r["use_count"] for r in users)
        last = _day(max(r["last_used_at"] or "" for r in users))
        who = [f"{_player_name(names.get(r['user_id'], 'unknown player'))} {r['use_count']}" for r in users]
        if len(users) == 1:
            block = (f"**/{name}** · {uses} use{'s' if uses != 1 else ''} · "
                     f"{_player_name(names.get(users[0]['user_id'], 'unknown player'))} · last {last}")
        else:
            shown = who[:NAMES_SHOWN] + ([f"+{len(who) - NAMES_SHOWN} more"] if len(who) > NAMES_SHOWN else [])
            block = f"**/{name}** · {uses} uses · {len(users)} players · last {last}\n-# " + " · ".join(shown)
        blocks.append(block)

    body = []
    used = len("\n".join(lines + [""] + footer)) + 60  # room for the "more" line
    for index, block in enumerate(blocks):
        if used + len(block) + 1 > REPORT_LIMIT:
            more = len(blocks) - index
            body.append(f"-# {more} more command{'s' if more != 1 else ''} with real use not shown")
            break
        body.append(block)
        used += len(block) + 1
    return "\n".join(lines + ([""] + body if body else []) + ([""] + footer if footer else []))


class Diagnostics(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="test-dm",
        description="Send yourself a test DM to check whether this bot can actually reach you that way.",
    )
    async def test_dm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            # interaction.user.send() creates/uses the same DM channel and hits the same
            # Discord permission checks as bot.fetch_user(user_id).send() - what every
            # background poller here uses to deliver a DM - so a pass/fail here is a direct
            # answer for whether those will reach this user too.
            await interaction.user.send(
                "This is a test DM from the UEX Trading Bot. If you're reading this, DMs from "
                "this bot reach you - any alert set to DM you (the default for /alert-add, "
                "/stock-alert-add and /marketplace-alert-add) will get through too."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't DM you - Discord blocked it (a 403 Forbidden). This almost always "
                "means one of two things: your Discord privacy settings have \"Allow direct "
                "messages from server members\" turned off for this server (right-click the "
                "server icon → Privacy Settings, or Server Settings → Privacy Settings depending "
                "on your client), or you've blocked this bot specifically. Fix whichever applies "
                "and run /test-dm again. Until then, an alert set to DM you is posted in the "
                "channel you set it in instead, with a ping.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Couldn't DM you - unexpected Discord error, not the usual privacy-settings block: {exc}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Sent! If it showed up in your DMs, this bot can reach you there - alerts set to "
            "DM you will arrive.",
            ephemeral=True,
        )

    @app_commands.command(
        name="command-usage",
        description="Owner-only: real command usage (excluding your own testing), to inform trimming.",
    )
    @app_commands.describe(command="Optional: see WHICH real users ran this specific command, for feedback outreach.")
    @app_commands.autocomplete(command=tracked_command_autocomplete)
    @app_commands.default_permissions(manage_guild=True)  # hidden from members (audit UX-17)
    async def command_usage(self, interaction: discord.Interaction, command: str | None = None) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Owner-only command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        # is_owner() above guarantees one of these is populated by now (it fetches
        # application_info() and sets whichever applies the first time it's ever called) -
        # owner_ids covers a team-owned application, where discord.py populates that set
        # instead of a single owner_id.
        owner_ids = self.bot.owner_ids or ({self.bot.owner_id} if self.bot.owner_id else set())

        if command is not None:
            users = await self.bot.db.get_command_users(command, owner_ids)
            if not users:
                await interaction.followup.send(
                    f"No recorded real (non-owner) usage for `/{command}`.", ephemeral=True,
                )
                return
            # Deliberately NOT a ```code block``` (unlike the aggregate report below) -
            # Discord does not parse <@id> mention syntax inside one, so the whole point of
            # embedding a clickable mention (jump straight to that user, no manual lookup)
            # would silently stop working while still looking fine at a glance.
            lines = [f"**Real users of /{command}** ({len(users)}):", ""]
            for u in users:
                # A stored Discord display name is player-controlled (their own nickname/
                # username) - audit-confirmed defect: sent unescaped, an `@everyone` or a
                # crafted <@id>/<@&id> mention embedded in someone's own display name would
                # actually ping when the owner ran this. escape_mentions neutralizes any
                # mention text hiding in the NAME; the real <@user_id> mention just below is
                # legitimate and is what allowed_mentions (see the send call) still permits.
                display_name = discord.utils.escape_mentions(
                    u["username"] or "(unknown name - hasn't used this command since usernames started being tracked)"
                )
                lines.append(
                    f"- {display_name} (<@{u['user_id']}>) - {u['use_count']} use(s), "
                    f"last {(u['last_used_at'] or '')[:10]}"
                )
            body = "\n".join(lines)
            if len(body) > 1990:
                body = body[:1950] + "\n... truncated ..."
            # Second, independent layer on top of escape_mentions above: even if a mention
            # slipped past that (or Discord's own parsing has a gap escape_mentions doesn't
            # cover), this caps what CAN ping to exactly the genuine user_ids this report is
            # listing - never a role or @everyone/@here, regardless of what's in body.
            await interaction.followup.send(
                body, ephemeral=True,
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=u["user_id"]) for u in users], everyone=False, roles=False,
                ),
            )
            return

        rows = await self.bot.db.get_real_command_usage(owner_ids)
        # walk_commands() is the live command surface right now: a command no longer in it
        # (e.g. /my-ship, retired 2026-09) still has rows, reported apart as retired.
        all_names = {cmd.qualified_name for cmd in self.bot.tree.walk_commands()}
        # Names are the players' own text (escaped in usage_report); nothing here may ping.
        await interaction.followup.send(usage_report(rows, all_names), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Diagnostics(bot))
