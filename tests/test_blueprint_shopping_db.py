import asyncio
from cryptography.fernet import Fernet
from bot.db.database import Database
from bot.uex.blueprint_crafting import Recipe
from tests.test_blueprint_crafting import rifle


def test_shopping_list_survives_restart_is_scoped_and_deduplicates_retries(tmp_path):
    async def run():
        path = tmp_path / 'shopping.sqlite'
        key = Fernet(Fernet.generate_key())
        db = Database(path, key)
        await db.init()
        plan = Recipe.parse(rifle()).plan(5, {}, {})
        await asyncio.gather(*(db.add_blueprint_plan(1, 10, 'same-request', plan) for _ in range(3)))
        assert len(await db.get_blueprint_plans(1, 10)) == 1
        assert await db.get_blueprint_plans(2, 10) == []
        assert await db.get_blueprint_plans(1, 20) == []
        await db.add_blueprint_plan(1, 10, 'other-request', plan)
        await db.set_blueprint_thread(1, 10, 100, 200)
        db = Database(path, key)
        await db.init()
        entries = await db.get_blueprint_plans(1, 10)
        assert len(entries) == 2
        assert entries[0]['plan'] == plan
        assert (await db.get_blueprint_thread(1, 10))['message_id'] == 200
        assert not await db.remove_blueprint_plan(2, 10, entries[0]['id'])
        assert await db.remove_blueprint_plan(1, 10, entries[0]['id'])
        await db.clear_blueprint_plans(1, 10)
        await db.init()
        assert await db.get_blueprint_plans(1, 10) == []
    asyncio.run(run())
