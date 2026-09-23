"""Diagnostic commands: /test-dm and /command-usage.

Motivated by a real, recurring problem with Discord bots in general: whether a bot's DM
actually reaches a given user depends on THEIR Discord settings (a per-server "Allow direct
messages from server members" toggle, or having blocked the bot outright), not anything the
bot does - and the failure looks identical from the bot's side either way (Discord just
returns a 403 Forbidden). This bot now has two DM-only delivery paths (Marketplace alerts, and
a Personal-scope /stock-alert-add) where a silently-failing DM means the user never finds out
their alert is dead. /test-dm lets someone check that channel actually works *before*
depending on it, rather than discovering it during a real alert.

/command-usage is owner-only: a running count of real command usage (bot.uex.main's
on_app_command_completion listener feeds command_usage_by_user), specifically to inform
trimming the command surface for new-user friendliness - see CONTRIBUTING.md/
PROJECT_CONTEXT.md. The owner's own constant testing is tracked but excluded from "real"
usage, since it would otherwise make every command look used regardless of whether any
actual player ever touches it. Its optional `command` option drills into who (by display
name, not just a count) has actually run one specific command, for reaching out to real
users for feedback.
"""
from __future__ import annotations

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
                "this bot reach you - any Personal-scope alert (/stock-alert-add ... scope: "
                "Personal) or Marketplace alert (/marketplace-alert-add) will get through too."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't DM you - Discord blocked it (a 403 Forbidden). This almost always "
                "means one of two things: your Discord privacy settings have \"Allow direct "
                "messages from server members\" turned off for this server (right-click the "
                "server icon → Privacy Settings, or Server Settings → Privacy Settings depending "
                "on your client), or you've blocked this bot specifically. Fix whichever applies "
                "and run /test-dm again - anything DM-only (Personal-scope stock alerts, "
                "Marketplace alerts) needs this to actually work for you.",
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
            "Sent! If it showed up in your DMs, this bot can reach you there - Personal-scope "
            "stock alerts and Marketplace alerts will work fine.",
            ephemeral=True,
        )

    @app_commands.command(
        name="command-usage",
        description="Owner-only: real command usage (excluding your own testing), to inform trimming.",
    )
    @app_commands.describe(command="Optional: see WHICH real users ran this specific command, for feedback outreach.")
    @app_commands.autocomplete(command=tracked_command_autocomplete)
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

        stats = await self.bot.db.get_command_usage_stats(owner_ids)
        # walk_commands() is the live command surface right now - a name in it with no
        # matching stats row has never been invoked by anyone, including the owner, which is
        # the strongest possible "nobody is touching this" signal for trimming.
        all_names = {cmd.qualified_name for cmd in self.bot.tree.walk_commands()}
        # Audit-confirmed defect: a command removed from the tree (e.g. /my-ship, retired
        # this same session) still has historical rows in command_usage_by_user. Nothing
        # filtered the report against the CURRENT command tree, so a retired command kept
        # appearing in least/most-used and inflated "have at least one recorded invocation"
        # below - split before building anything, and surface retired ones on their own
        # labeled line instead of silently dropping that history.
        live_stats = [row for row in stats if row["command_name"] in all_names]
        retired_stats = [row for row in stats if row["command_name"] not in all_names]
        tracked_names = {row["command_name"] for row in live_stats}
        never_invoked = sorted(all_names - tracked_names)

        rows = [
            {
                "name": row["command_name"],
                "real": row["total_count"] - row["owner_count"],
                "owner": row["owner_count"],
                "users": row["distinct_real_users"],
                "last_real": (row["last_used_excluding_owner_at"] or "never")[:10],
            }
            for row in live_stats
        ]
        least_used = sorted(rows, key=lambda r: r["real"])[:15]
        most_used = sorted(rows, key=lambda r: -r["real"])[:10]

        lines = [
            f"{len(all_names)} live commands - {len(tracked_names)} have at least one recorded "
            f"invocation, {len(never_invoked)} have never been invoked at all (not even by you).",
            "",
            "Least used (real usage, your own testing excluded):",
        ]
        for r in least_used:
            lines.append(
                f"  /{r['name']:<28} {r['real']:>4} real  {r['users']:>3} users  "
                f"({r['owner']} by you, last real: {r['last_real']})"
            )
        if never_invoked:
            lines.append("")
            lines.append("Never invoked at all: " + ", ".join(f"/{n}" for n in never_invoked))
        if retired_stats:
            lines.append("")
            retired_summary = ", ".join(
                f"/{row['command_name']} ({row['total_count'] - row['owner_count']} real)"
                for row in retired_stats
            )
            lines.append(f"Retired (no longer a live command, kept for history): {retired_summary}")
        lines.append("")
        lines.append("Most used:")
        for r in most_used:
            lines.append(
                f"  /{r['name']:<28} {r['real']:>4} real  {r['users']:>3} users  "
                f"({r['owner']} by you, last real: {r['last_real']})"
            )

        body = "```\n" + "\n".join(lines) + "\n```"
        # Discord's non-embed message cap is 2000 chars, well under this table's worst case
        # (67 commands x ~2 sections) - truncate with a visible note rather than let the send
        # itself fail, matching this codebase's established "disclose, don't silently drop"
        # convention for anything that can overflow a Discord limit.
        if len(body) > 1990:
            body = body[:1900] + "\n... truncated, ask again after some commands are trimmed ...\n```"
        await interaction.followup.send(body, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Diagnostics(bot))
