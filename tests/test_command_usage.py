"""Tests for command usage tracking: Database.record_command_usage/get_command_usage_stats/
get_command_users, UexBot.on_app_command_completion (bot/main.py), and /command-usage
(bot/cogs/diagnostics.py)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.cogs.diagnostics import Diagnostics, tracked_command_autocomplete
from bot.db.database import Database
from bot.main import UexBot

OWNER_ID = 999


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "command_usage.sqlite3", Fernet(Fernet.generate_key()))


# -- Database.record_command_usage / get_command_usage_stats -----------------------------

def test_record_command_usage_accumulates_per_command_and_user(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        await db.record_command_usage("best-route", OWNER_ID, "TheOwner")
        await db.record_command_usage("best-route", OWNER_ID, "TheOwner")
        await db.record_command_usage("best-route", 111, "PlayerOne")

        stats = await db.get_command_usage_stats({OWNER_ID})
        assert len(stats) == 1
        row = stats[0]
        assert row["command_name"] == "best-route"
        assert row["total_count"] == 3
        assert row["owner_count"] == 2
        assert row["distinct_real_users"] == 1

    asyncio.run(run())


def test_record_command_usage_refreshes_username_on_every_call(tmp_path):
    """username is overwritten each time, not set-once - it should always reflect the
    user's CURRENT display name as of their last use of this command, since the whole
    point of storing it is recognizing who to reach out to right now."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        await db.record_command_usage("price", 111, "OldName")
        await db.record_command_usage("price", 111, "NewName")

        users = await db.get_command_users("price", {OWNER_ID})
        assert len(users) == 1
        assert users[0]["username"] == "NewName"
        assert users[0]["use_count"] == 2

    asyncio.run(run())


def test_get_command_usage_stats_last_used_excluding_owner_reflects_only_real_use(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        await db.record_command_usage("mixed-routes", 111, "PlayerOne")
        first = (await db.get_command_usage_stats({OWNER_ID}))[0]
        first_real_ts = first["last_used_excluding_owner_at"]
        assert first_real_ts is not None

        # A later owner-only call must not blank out or move the last REAL usage timestamp -
        # that field exists specifically to answer "when did a real player last use this,"
        # which an owner test run has no business changing.
        await db.record_command_usage("mixed-routes", OWNER_ID, "TheOwner")
        second = (await db.get_command_usage_stats({OWNER_ID}))[0]
        assert second["last_used_excluding_owner_at"] == first_real_ts
        assert second["total_count"] == 2
        assert second["owner_count"] == 1

    asyncio.run(run())


def test_get_command_usage_stats_owner_only_command_has_no_real_usage_timestamp(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        await db.record_command_usage("blueprint-search", OWNER_ID, "TheOwner")

        row = (await db.get_command_usage_stats({OWNER_ID}))[0]
        assert row["last_used_excluding_owner_at"] is None, (
            "a command only ever invoked by the owner has no real usage yet"
        )
        assert row["distinct_real_users"] == 0

    asyncio.run(run())


def test_get_command_usage_stats_orders_by_real_usage_descending(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        # "heavy" has more real usage than "light", despite being recorded first and having
        # a lower total_count than "owner-heavy" (which is almost all owner testing).
        await db.record_command_usage("light", 111, "PlayerOne")
        for user_id in (111, 222, 333, 444, 555):
            await db.record_command_usage("heavy", user_id, f"Player{user_id}")
        for _ in range(10):
            await db.record_command_usage("owner-heavy", OWNER_ID, "TheOwner")
        await db.record_command_usage("owner-heavy", 111, "PlayerOne")

        names_in_order = [row["command_name"] for row in await db.get_command_usage_stats({OWNER_ID})]
        assert names_in_order == ["heavy", "light", "owner-heavy"], names_in_order

    asyncio.run(run())


def test_get_command_usage_stats_counts_distinct_real_users_not_total_invocations(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        # Same user calling twice is still ONE distinct real user - distinct_real_users
        # answers "how many different people," not "how many times."
        await db.record_command_usage("price", 111, "PlayerOne")
        await db.record_command_usage("price", 111, "PlayerOne")
        await db.record_command_usage("price", 222, "PlayerTwo")

        row = (await db.get_command_usage_stats({OWNER_ID}))[0]
        assert row["total_count"] == 3
        assert row["distinct_real_users"] == 2

    asyncio.run(run())


def test_get_command_usage_stats_handles_multiple_owner_ids(tmp_path):
    """owner_ids is a set, not a single id, to cover a team-owned Discord application where
    discord.py populates bot.owner_ids instead of a single bot.owner_id."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        await db.record_command_usage("price", 1, "OwnerA")
        await db.record_command_usage("price", 2, "OwnerB")
        await db.record_command_usage("price", 3, "PlayerOne")

        row = (await db.get_command_usage_stats({1, 2}))[0]
        assert row["total_count"] == 3
        assert row["owner_count"] == 2
        assert row["distinct_real_users"] == 1

    asyncio.run(run())


# -- Database.get_command_users -----------------------------------------------------------

def test_get_command_users_excludes_owner_ids_and_ranks_by_use_count(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()

        for _ in range(5):
            await db.record_command_usage("best-route", OWNER_ID, "TheOwner")
        await db.record_command_usage("best-route", 111, "PlayerOne")
        for _ in range(3):
            await db.record_command_usage("best-route", 222, "PlayerTwo")

        users = await db.get_command_users("best-route", {OWNER_ID})

        assert [u["user_id"] for u in users] == [222, 111], "ranked by use_count descending"
        assert all(u["user_id"] != OWNER_ID for u in users)
        assert users[0]["username"] == "PlayerTwo"
        assert users[0]["use_count"] == 3

    asyncio.run(run())


def test_get_command_users_returns_nothing_for_a_command_only_the_owner_used(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_command_usage("blueprint-search", OWNER_ID, "TheOwner")

        assert await db.get_command_users("blueprint-search", {OWNER_ID}) == []

    asyncio.run(run())


def test_get_command_users_returns_nothing_for_an_unknown_command(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        await db.record_command_usage("best-route", 111, "PlayerOne")

        assert await db.get_command_users("some-other-command", {OWNER_ID}) == []

    asyncio.run(run())


# -- UexBot.on_app_command_completion (bot/main.py) ---------------------------------------

def test_on_app_command_completion_records_the_command_user_and_display_name():
    async def run():
        bot = UexBot.__new__(UexBot)
        bot.db = NS(record_command_usage=AsyncMock())
        command = NS(qualified_name="best-route")
        interaction = NS(user=NS(id=111, display_name="PlayerOne"))

        await bot.on_app_command_completion(interaction, command)

        bot.db.record_command_usage.assert_awaited_once_with("best-route", 111, "PlayerOne")

    asyncio.run(run())


def test_on_app_command_completion_swallows_a_recording_failure():
    """Best-effort only: by the time this listener fires, the command's own response has
    already been sent - a failure here must never surface to the user or crash the bot."""
    async def run():
        bot = UexBot.__new__(UexBot)
        bot.db = NS(record_command_usage=AsyncMock(side_effect=RuntimeError("db is locked")))
        command = NS(qualified_name="mixed-routes")
        interaction = NS(user=NS(id=222, display_name="PlayerTwo"))

        await bot.on_app_command_completion(interaction, command)  # must not raise

    asyncio.run(run())


# -- tracked_command_autocomplete (bot/cogs/diagnostics.py) -------------------------------

def test_tracked_command_autocomplete_filters_by_current_input():
    async def run():
        stats = [{"command_name": "best-route"}, {"command_name": "mixed-routes"}, {"command_name": "price"}]
        interaction = NS(client=NS(
            owner_ids=set(), owner_id=OWNER_ID,
            db=NS(get_command_usage_stats=AsyncMock(return_value=stats)),
        ))

        choices = await tracked_command_autocomplete(interaction, "route")

        assert {c.value for c in choices} == {"best-route", "mixed-routes"}

    asyncio.run(run())


def test_tracked_command_autocomplete_swallows_failures():
    async def run():
        interaction = NS(client=NS(
            owner_ids=set(), owner_id=OWNER_ID,
            db=NS(get_command_usage_stats=AsyncMock(side_effect=RuntimeError("db down"))),
        ))

        assert await tracked_command_autocomplete(interaction, "") == []

    asyncio.run(run())


# -- /command-usage (bot/cogs/diagnostics.py) ---------------------------------------------

class _FakeInteraction:
    def __init__(self, *, user_id: int = OWNER_ID) -> None:
        self.user = NS(id=user_id)
        self.response = NS(defer=AsyncMock(), send_message=AsyncMock())
        self.followup = NS(send=AsyncMock())


def _cog(*, is_owner: bool, stats, live_command_names, owner_id=OWNER_ID, owner_ids=None, users_by_command=None):
    cog = Diagnostics.__new__(Diagnostics)
    cog.bot = NS(
        is_owner=AsyncMock(return_value=is_owner),
        owner_id=owner_id,
        owner_ids=owner_ids or set(),
        db=NS(
            get_command_usage_stats=AsyncMock(return_value=stats),
            get_command_users=AsyncMock(side_effect=lambda name, _owner_ids: (users_by_command or {}).get(name, [])),
        ),
        tree=NS(walk_commands=lambda: [NS(qualified_name=name) for name in live_command_names]),
    )
    return cog


def _usage_row(name, *, total, owner, users=0, last_real=None):
    return {
        "command_name": name, "total_count": total, "owner_count": owner,
        "distinct_real_users": users, "last_used_at": "2026-09-22 00:00:00",
        "last_used_excluding_owner_at": last_real,
    }


def _user_row(user_id, username, *, use_count, last_used_at="2026-09-22 00:00:00"):
    return {"user_id": user_id, "username": username, "use_count": use_count, "last_used_at": last_used_at}


def test_command_usage_rejects_a_non_owner_without_deferring():
    async def run():
        cog = _cog(is_owner=False, stats=[], live_command_names=[])
        interaction = _FakeInteraction(user_id=111)

        await cog.command_usage.callback(cog, interaction, None)

        interaction.response.send_message.assert_awaited_once()
        assert "owner" in interaction.response.send_message.call_args.args[0].lower()
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    asyncio.run(run())


def test_command_usage_reports_real_usage_and_never_invoked_commands():
    async def run():
        stats = [
            _usage_row("best-route", total=12, owner=2, users=3, last_real="2026-09-22 01:00:00"),
            _usage_row("blueprint-search", total=5, owner=5, users=0, last_real=None),
        ]
        cog = _cog(
            is_owner=True, stats=stats,
            live_command_names=["best-route", "blueprint-search", "never-used-command"],
        )
        interaction = _FakeInteraction()

        await cog.command_usage.callback(cog, interaction, None)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        cog.bot.db.get_command_usage_stats.assert_awaited_once_with({OWNER_ID})
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        body = interaction.followup.send.call_args.args[0]
        assert kwargs.get("ephemeral") is True
        assert "/best-route" in body and "10 real" in body, body  # 12 total - 2 owner = 10 real
        assert "3 users" in body
        assert "/blueprint-search" in body
        assert "never-used-command" in body
        assert "3 live commands" in body

    asyncio.run(run())


def test_command_usage_resolves_owner_ids_set_when_owner_id_is_unset():
    """A team-owned Discord application: discord.py populates bot.owner_ids instead of a
    single bot.owner_id."""
    async def run():
        stats = [_usage_row("best-route", total=5, owner=1, users=2, last_real="2026-09-22 00:00:00")]
        cog = _cog(
            is_owner=True, stats=stats, live_command_names=["best-route"],
            owner_id=None, owner_ids={10, 20},
        )
        interaction = _FakeInteraction()

        await cog.command_usage.callback(cog, interaction, None)

        cog.bot.db.get_command_usage_stats.assert_awaited_once_with({10, 20})

    asyncio.run(run())


def test_command_usage_truncates_instead_of_exceeding_discords_message_limit():
    """Only 25 rows ever render (least_used[:15] + most_used[:10]), so short names alone
    might not actually exceed the 2000-char cap - long, realistic-length names make sure
    this test actually exercises the truncation branch, not just the untruncated path."""
    async def run():
        stats = [
            _usage_row(
                f"a-very-long-realistic-command-name-{i:03d}", total=i + 1, owner=0, users=i,
                last_real="2026-09-22 00:00:00",
            )
            for i in range(80)
        ]
        cog = _cog(is_owner=True, stats=stats, live_command_names=[row["command_name"] for row in stats])
        interaction = _FakeInteraction()

        await cog.command_usage.callback(cog, interaction, None)

        body = interaction.followup.send.call_args.args[0]
        assert len(body) <= 2000, f"Discord's non-embed message cap is 2000 chars, got {len(body)}"
        assert "truncated" in body, "expected the truncation branch to actually fire for this input"
        assert body.rstrip().endswith("```"), "truncation must still close the code block"

    asyncio.run(run())


def test_command_usage_with_a_command_shows_the_real_users_who_ran_it():
    async def run():
        users_by_command = {
            "best-route": [
                _user_row(222, "PlayerTwo", use_count=3),
                _user_row(111, "PlayerOne", use_count=1),
            ],
        }
        cog = _cog(
            is_owner=True, stats=[], live_command_names=["best-route"],
            users_by_command=users_by_command,
        )
        interaction = _FakeInteraction()

        await cog.command_usage.callback(cog, interaction, "best-route")

        cog.bot.db.get_command_users.assert_awaited_once_with("best-route", {OWNER_ID})
        cog.bot.db.get_command_usage_stats.assert_not_awaited()
        body = interaction.followup.send.call_args.args[0]
        assert "PlayerTwo" in body and "<@222>" in body
        assert "PlayerOne" in body and "<@111>" in body
        assert "3 use" in body

    asyncio.run(run())


def test_command_usage_with_a_command_no_real_usage_says_so():
    async def run():
        cog = _cog(is_owner=True, stats=[], live_command_names=["blueprint-search"], users_by_command={})
        interaction = _FakeInteraction()

        await cog.command_usage.callback(cog, interaction, "blueprint-search")

        body = interaction.followup.send.call_args.args[0]
        assert "No recorded real" in body
        assert "blueprint-search" in body

    asyncio.run(run())
