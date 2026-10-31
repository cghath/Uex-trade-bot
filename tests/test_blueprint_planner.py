import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import discord

from bot.cogs import blueprint_planner
from bot.cogs.blueprint_planner import ShoppingService, ShoppingView
from bot.db.database import Database
from bot.uex.blueprint_crafting import Recipe


def _plan():
    raw = json.loads((Path(__file__).parent / "fixtures" / "blueprint_crafting_rifle.json").read_text(encoding="utf-8-sig"))
    return Recipe.parse(raw).plan(5, {}, {"0.0": 521})


class FakeMessage:
    id = 200

    def __init__(self):
        self.edit = AsyncMock()


class FakeThread:
    id = 100
    mention = "<#100>"
    archived = False

    def __init__(self, *, fail_add=False):
        self.message = FakeMessage()
        self.add_user = AsyncMock(side_effect=RuntimeError("add failed") if fail_add else None)
        self.send = AsyncMock(return_value=self.message)
        self.fetch_message = AsyncMock(return_value=self.message)
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeChannel:
    def __init__(self, thread):
        self.thread = thread
        self.create_thread = AsyncMock(return_value=thread)


def _interaction(channel, *, user_id=1, interaction_id=300):
    return NS(
        id=interaction_id, guild_id=10, channel=channel,
        user=NS(id=user_id, display_name="Pilot"),
        response=NS(send_message=AsyncMock(), defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


def test_complete_private_thread_add_and_restart_reuse(tmp_path, monkeypatch):
    async def run():
        path, key = tmp_path / "planner.sqlite", Fernet(Fernet.generate_key())
        db = Database(path, key)
        await db.init()
        thread = FakeThread()
        channel = FakeChannel(thread)
        monkeypatch.setattr(blueprint_planner.discord, "TextChannel", FakeChannel)
        monkeypatch.setattr(blueprint_planner.discord, "Thread", FakeThread)
        bot = NS(db=db, get_channel=lambda _id: None, fetch_channel=AsyncMock())
        interaction = _interaction(channel)
        assert await ShoppingService(bot).add(interaction, _plan())
        first = await db.get_blueprint_plans(1, 10)

        restarted = Database(path, key)
        await restarted.init()
        bot2 = NS(db=restarted, get_channel=lambda _id: thread, fetch_channel=AsyncMock())
        await ShoppingService(bot2).open(_interaction(channel, interaction_id=301))
        return first, channel, thread, restarted

    entries, channel, thread, restarted = asyncio.run(run())
    assert len(entries) == 1 and entries[0]["plan"]["blueprint_uuid"] == _plan()["blueprint_uuid"]
    assert channel.create_thread.await_count == 1, "restart reuses the saved private thread"
    assert thread.add_user.await_count == 2
    assert asyncio.run(restarted.get_blueprint_thread(1, 10))["thread_id"] == 100


def test_partial_thread_setup_is_deleted_and_does_not_add_a_plan(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path / "failed.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        thread = FakeThread(fail_add=True)
        channel = FakeChannel(thread)
        monkeypatch.setattr(blueprint_planner.discord, "TextChannel", FakeChannel)
        bot = NS(db=db, get_channel=lambda _id: None, fetch_channel=AsyncMock())
        interaction = _interaction(channel)
        saved = await ShoppingService(bot).add(interaction, _plan())
        return saved, await db.get_blueprint_plans(1, 10), await db.get_blueprint_thread(1, 10), thread, interaction

    saved, plans, mapping, thread, interaction = asyncio.run(run())
    assert not saved and plans == [] and mapping is None
    thread.delete.assert_awaited_once()
    assert "couldn't create" in interaction.followup.send.await_args.args[0]


def test_persistent_controls_reject_a_non_owner(tmp_path):
    async def run():
        db = Database(tmp_path / "owner.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_blueprint_thread(1, 10, 100, 200)
        service = ShoppingService(NS(db=db))
        interaction = _interaction(NS(id=100), user_id=2)
        row = await ShoppingView(service)._owner(interaction)
        return row, interaction

    row, interaction = asyncio.run(run())
    assert row is None
    interaction.response.send_message.assert_awaited_once_with(
        "This shopping list belongs to another player.", ephemeral=True,
    )


def test_oversized_private_list_uses_one_safe_attachment():
    async def run():
        thread = FakeThread()
        db = NS(get_blueprint_thread=AsyncMock(return_value={"message_id": 200}))
        service = ShoppingService(NS(db=db))
        service.render = AsyncMock(return_value=["x" * 1900, "@everyone second page"])
        await service.refresh(thread, 1, 10)
        return thread

    thread = asyncio.run(run())
    kwargs = thread.message.edit.await_args.kwargs
    assert len(kwargs["content"]) < 2000 and kwargs["allowed_mentions"] == blueprint_planner.NO_MENTIONS
    assert len(kwargs["attachments"]) == 1
    assert kwargs["attachments"][0].filename == "blueprint-shopping-list.txt"


# -- a Discord failure after the defer must still get a reply, not "the application did not respond" ----------

def _discord_failure(kind):
    response = NS(status=403 if kind is discord.Forbidden else 500, reason="test")
    return kind(response, "simulated failure")


async def _owned_view(tmp_path, name):
    db = Database(tmp_path / name, Fernet(Fernet.generate_key()))
    await db.init()
    await db.set_blueprint_thread(1, 10, 100, 200)
    service = ShoppingService(NS(db=db))
    return db, service, ShoppingView(service)


def test_opening_the_list_answers_when_the_thread_update_fails():
    async def run():
        for kind in (discord.Forbidden, discord.HTTPException):
            service = ShoppingService(NS(db=NS()))
            service._thread = AsyncMock(return_value=FakeThread())
            service.refresh = AsyncMock(side_effect=_discord_failure(kind))
            interaction = _interaction(NS(id=100))
            await service.open(interaction)
            interaction.followup.send.assert_awaited_once()
            assert "couldn't update" in interaction.followup.send.await_args.args[0], kind


def test_adding_a_plan_answers_when_the_thread_refresh_fails_with_a_non_discord_error():
    """Audit-confirmed defect: add()'s post-save refresh call only caught
    discord.Forbidden/discord.HTTPException around the SAME refresh() call open() makes a
    few lines below with a broad except Exception - a DB read-back error, a malformed
    stored plan, or any other non-Discord rendering failure escaped add()'s narrower catch
    uncaught, leaving the interaction unanswered ("the application did not respond") even
    though the plan was already saved."""
    async def run():
        service = ShoppingService(NS(db=NS(add_blueprint_plan=AsyncMock())))
        service._thread = AsyncMock(return_value=FakeThread())
        service.refresh = AsyncMock(side_effect=RuntimeError("not a discord.HTTPException/Forbidden"))
        interaction = _interaction(NS(id=100))
        saved = await service.add(interaction, _plan())
        return saved, interaction

    saved, interaction = asyncio.run(run())
    assert saved is True, "the plan was already saved before the refresh failed"
    interaction.followup.send.assert_awaited_once()
    assert "Saved, but I couldn't refresh" in interaction.followup.send.await_args.args[0]

    asyncio.run(run())


def test_refresh_button_answers_when_the_refresh_fails(tmp_path):
    async def run():
        _, service, view = await _owned_view(tmp_path, "refresh_fail.sqlite")
        service.refresh = AsyncMock(side_effect=_discord_failure(discord.Forbidden))
        interaction = _interaction(NS(id=100), user_id=1)
        await view.refresh_button.callback(interaction)
        interaction.followup.send.assert_awaited_once()
        assert "couldn't refresh" in interaction.followup.send.await_args.args[0]

    asyncio.run(run())


def test_clear_button_says_so_when_the_list_was_cleared_but_the_message_could_not_be_updated(tmp_path):
    async def run():
        db, service, view = await _owned_view(tmp_path, "clear_refresh_fail.sqlite")
        await db.add_blueprint_plan(1, 10, "req-1", {"plan": "x"})
        service.refresh = AsyncMock(side_effect=_discord_failure(discord.HTTPException))
        interaction = _interaction(NS(id=100), user_id=1)
        await view.clear_button.callback(interaction)
        interaction.followup.send.assert_awaited_once()
        assert "was cleared" in interaction.followup.send.await_args.args[0]
        assert await db.get_blueprint_plans(1, 10) == [], "the clear itself must still have happened"

    asyncio.run(run())


def test_clear_button_says_nothing_changed_when_the_clear_itself_fails(tmp_path):
    async def run():
        db, service, view = await _owned_view(tmp_path, "clear_db_fail.sqlite")
        db.clear_blueprint_plans = AsyncMock(side_effect=RuntimeError("database is locked"))
        service.refresh = AsyncMock()
        interaction = _interaction(NS(id=100), user_id=1)
        await view.clear_button.callback(interaction)
        interaction.followup.send.assert_awaited_once()
        assert "Nothing was changed" in interaction.followup.send.await_args.args[0]
        service.refresh.assert_not_awaited()

    asyncio.run(run())


# -- the 2026-10-30 audit's shopping-list findings (BP-6..BP-11) --------------------------------------

async def _service_with_thread(tmp_path, monkeypatch, name):
    db = Database(tmp_path / name, Fernet(Fernet.generate_key()))
    await db.init()
    thread = FakeThread()
    channel = FakeChannel(thread)
    monkeypatch.setattr(blueprint_planner.discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(blueprint_planner.discord, "Thread", FakeThread)
    return db, channel, thread, ShoppingService(NS(db=db, get_channel=lambda _id: None, fetch_channel=AsyncMock()))


def test_a_double_click_on_add_saves_the_plan_once(tmp_path, monkeypatch):
    """Audit BP-6: every click is its own interaction, so the request_id dedupe never caught a second
    click and the combined amounts doubled."""
    async def run():
        db, channel, _, service = await _service_with_thread(tmp_path, monkeypatch, "double.sqlite")
        first, second = _interaction(channel, interaction_id=301), _interaction(channel, interaction_id=302)
        results = await asyncio.gather(service.add(first, _plan()), service.add(second, _plan()))
        return results, await db.get_blueprint_plans(1, 10), second

    results, plans, second = asyncio.run(run())
    assert results == [True, False] and len(plans) == 1
    assert "Already adding" in second.followup.send.await_args.args[0]


def test_add_from_inside_a_thread_starts_the_list_in_that_threads_channel(tmp_path, monkeypatch):
    """Audit BP-8: from a thread nothing was saved and the player was told to check thread permissions."""
    async def run():
        db, channel, _, service = await _service_with_thread(tmp_path, monkeypatch, "in_thread.sqlite")
        inside = FakeThread()
        inside.parent = channel
        saved = await service.add(_interaction(inside), _plan())
        return saved, channel, await db.get_blueprint_plans(1, 10)

    saved, channel, plans = asyncio.run(run())
    assert saved and len(plans) == 1
    channel.create_thread.assert_awaited_once()


def test_add_from_a_forum_post_or_voice_chat_says_why_not_thread_permissions(tmp_path, monkeypatch):
    async def run():
        db, _, _, service = await _service_with_thread(tmp_path, monkeypatch, "voice.sqlite")
        forum_post = FakeThread()
        forum_post.parent = NS(id=5)  # a forum channel, not a text channel
        answers = []
        for channel in (NS(id=7), forum_post):  # a voice channel's chat, then a forum post
            interaction = _interaction(channel)
            assert not await service.add(interaction, _plan())
            answers.append(interaction.followup.send.await_args.args[0])
            interaction = _interaction(channel)
            await service.open(interaction)
            answers.append(interaction.followup.send.await_args.args[0])
        return answers, await db.get_blueprint_plans(1, 10)

    answers, plans = asyncio.run(run())
    assert plans == [] and all(answer == blueprint_planner.NO_THREAD_HERE for answer in answers)
    assert "permissions" not in blueprint_planner.NO_THREAD_HERE


def test_an_old_lists_buttons_say_it_is_no_longer_in_use_not_another_players(tmp_path):
    """Audit BP-9: when the saved thread couldn't be reopened, a new one replaced it, and the old one's
    buttons told its own owner the list 'belongs to another player'."""
    async def run():
        db = Database(tmp_path / "orphan.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        await db.set_blueprint_thread(1, 10, 101, 201)  # the list moved to thread 101
        interaction = _interaction(NS(id=100), user_id=1)
        row = await ShoppingView(ShoppingService(NS(db=db)))._owner(interaction)
        return row, interaction

    row, interaction = asyncio.run(run())
    assert row is None
    message = interaction.response.send_message.await_args.args[0]
    assert "another player" not in message and "/blueprint-list" in message


def _choice_plan(choice: int, qualities: dict):
    raw = json.loads((Path(__file__).parent / "fixtures" / "blueprint_crafting_rifle.json").read_text(encoding="utf-8-sig"))
    alternative = dict(raw["requirement_groups"][0]["children"][0], uuid="tin", name="Tin")
    raw["requirement_groups"][0]["children"].append(alternative)
    raw["requirement_groups"][0]["required_count"] = 1
    return Recipe.parse(raw).plan(1, {"0": [choice]}, qualities)


def test_the_list_names_materials_and_their_qualities_never_recipe_paths(tmp_path):
    """Audit BP-7: a quality picked for Iron, then Frame switched to Tin, stayed in the plan, and the line
    read 'choices 0=[1] · qualities 0.0=1000, 1.0=500'."""
    async def run():
        db = Database(tmp_path / "names.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        await db.add_blueprint_plan(1, 10, "r1", _choice_plan(1, {"0.0": 1000, "1.0": 500}))
        return "\n".join(await ShoppingService(NS(db=db)).render(1, 10))

    text = asyncio.run(run())
    line = next(line for line in text.splitlines() if line.startswith("• #"))
    assert "chose Tin (Frame)" in line and "quality Hephaestanite (Stock) 500" in line
    assert "0.0=" not in line and "1000" not in line and "choices" not in line, line


def test_a_plan_from_an_older_patch_says_so(tmp_path):
    async def run():
        db = Database(tmp_path / "stale.sqlite", Fernet(Fernet.generate_key()))
        await db.init()
        await db.add_blueprint_plan(1, 10, "r1", _plan())  # the fixture's game 4.10.0
        service = ShoppingService(NS(db=db))
        db.get_blueprint_snapshot_state = AsyncMock(return_value=NS(game_version="4.11.0-LIVE.2"))
        stale = "\n".join(await service.render(1, 10))
        db.get_blueprint_snapshot_state = AsyncMock(return_value=NS(game_version="4.10.0-LIVE.9"))
        return stale, "\n".join(await service.render(1, 10))

    stale, current = asyncio.run(run())
    assert "game 4.10.0 (before 4.11.0: its amounts may have changed)" in stale
    assert "may have changed" not in current and "LIVE" not in current, "a rebuild of the same patch isn't older"
