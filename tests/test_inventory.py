"""Personal inventory pricing, timing, persistence, and posting safety tests."""
from __future__ import annotations

import asyncio
import ast
from datetime import datetime, timedelta, timezone
import inspect

from cryptography.fernet import Fernet
import discord
from discord.ext import commands
import httpx

from bot.cogs.personal_inventory import PersonalInventory
import bot.cogs.personal_inventory as personal_inventory_module
from bot.db.database import Database
from bot.main import INITIAL_COGS
from bot.uex.client import UexClient
from bot.uex.inventory import (
    build_inventory_listing_payload,
    extract_listing_id,
    next_posting_time,
    recommend_balanced_price,
    recommend_posting_window,
)


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "inventory.sqlite3", Fernet(Fernet.generate_key()))


def test_personal_inventory_cog_is_registered_and_exposes_its_commands():
    async def run():
        bot = commands.Bot(command_prefix="!unused", intents=discord.Intents.none())
        try:
            await bot.add_cog(PersonalInventory(bot))
            assert {command.name for command in bot.tree.get_commands()} == {
                "inventory-add",
                "inventory",
                "inventory-set-minimum",
                "inventory-remove",
                "inventory-sell",
                "best-posting-time",
                "inventory-confirm-sale",
                "inventory-cancel-post",
            }
        finally:
            await bot.remove_cog("PersonalInventory")
            await bot.close()

    assert "bot.cogs.personal_inventory" in INITIAL_COGS
    asyncio.run(run())


def test_every_inventory_interaction_message_is_explicitly_ephemeral():
    tree = ast.parse(inspect.getsource(personal_inventory_module))
    interaction_sends = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"send_message", "send"}:
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Attribute) or owner.attr not in {"response", "followup"}:
            continue
        interaction_sends.append(node)
    assert interaction_sends
    assert all(
        any(
            keyword.arg == "ephemeral"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
        for call in interaction_sends
    )


def test_listing_id_uses_documented_id_listing_field():
    assert extract_listing_id({"id_listing": "431"}) == 431
    assert extract_listing_id({"id": 431}) is None
    assert extract_listing_id(None) is None


def test_item_catalog_loads_required_categories_once_and_deduplicates():
    async def run():
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path.endswith("/categories"):
                return httpx.Response(200, json={"status": "ok", "data": [{"id": 1}, {"id": 2}]})
            category_id = request.url.params.get("id_category")
            rows = {
                "1": [{"id": 10, "id_category": 1, "name": "Alpha"}],
                "2": [
                    {"id": 10, "id_category": 1, "name": "Alpha"},
                    {"id": 20, "id_category": 2, "name": "Beta"},
                ],
            }[category_id]
            return httpx.Response(200, json={"status": "ok", "data": rows})

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            first = await client.get_item_catalog()
            second = await client.get_item_catalog()
        finally:
            await client.aclose()
        assert [(row["id"], row["name"]) for row in first] == [(10, "Alpha"), (20, "Beta")]
        assert second == first
        assert len(calls) == 3

    asyncio.run(run())


def test_balanced_price_uses_matching_evidence_and_never_crosses_manual_floor():
    recommendation = recommend_balanced_price(
        listings=[
            {"operation": "sell", "price": "1200", "currency": "UEC", "unit": "unit", "quality": 0, "is_sold_out": 0},
            {"operation": "sell", "price": "1300", "currency": "UEC", "unit": "unit", "quality": 0, "is_sold_out": "0"},
            {"operation": "sell", "price": "1500", "currency": "UEC", "unit": "unit", "quality": 0, "is_sold_out": "1"},
            {"operation": "buy", "price": "1000", "currency": "UEC", "unit": "unit", "quality": 0, "is_sold_out": 0},
            # Wrong quality tier: must not affect a Q0 stack.
            {"operation": "sell", "price": "999999", "currency": "UEC", "unit": "unit", "quality": 950, "is_sold_out": 0},
        ],
        average_rows=[
            {
                "quality_tier": 0,
                "operation": "sell",
                "currency": "UEC",
                "unit": "unit",
                "price_avg": "1400",
                "price_avg_week": "1450",
                "price_avg_month": "1500",
            }
        ],
        quality=0,
        unit="unit",
        minimum_price=1425,
    )
    assert recommendation.price == 1425
    assert recommendation.floor_applied is True
    assert "recent sold-out asking prices" in recommendation.evidence
    assert "current competing sell asks" in recommendation.evidence
    assert recommendation.confidence == "High"


def test_balanced_price_falls_back_to_manual_floor_when_market_has_no_match():
    recommendation = recommend_balanced_price(
        listings=[], average_rows=[], quality=700, unit="scu", minimum_price=2500
    )
    assert recommendation.price == 2500
    assert recommendation.confidence == "Low"
    assert recommendation.evidence == ()


def test_auto_listing_payload_is_catalogued_uec_sell_only_and_expires_in_48_hours():
    payload = build_inventory_listing_payload(
        {
            "id_item": 7,
            "id_category": 9,
            "item_name": "FS-9 LMG (Gold)",
            "quality": 950,
            "unit": "unit",
            "location": "Area18",
            "notes": "Meet at the cargo deck.",
        },
        quantity=3,
        price=18_500,
    )
    assert payload["operation"] == "sell"
    assert payload["type"] == "item"
    assert payload["currency"] == "UEC"
    assert payload["id_item"] == 7
    assert payload["in_stock"] == 3
    assert payload["hours_expiration"] == 48
    assert payload["price"] == 18_500
    assert "(" not in payload["title"]
    assert "acquisition" not in str(payload).lower()
    assert "Meet at the cargo deck" not in payload["description"]


def _timing_rows(days: int, *, id_item: int = 1) -> list[dict]:
    rows = []
    successful = 0
    opened = 0
    listings = 10
    # January is UTC-5 in New York. Store local hourly observations as UTC.
    start_local_as_utc = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    for hour_index in range(days * 24):
        utc_time = start_local_as_utc + timedelta(hours=hour_index)
        local_hour = (utc_time.hour - 5) % 24
        if local_hour == 16:
            successful += 5
            opened += 4
        elif local_hour == 12:
            successful += 2
            listings += 4
        rows.append(
            {
                "id_item": id_item,
                "recorded_hour": utc_time.isoformat(),
                "negotiations_success": successful,
                "negotiations_open": opened,
                "listings_count_sell": listings,
            }
        )
    return rows


def test_posting_window_uses_item_history_after_seven_days_and_handles_eastern_time():
    rows = _timing_rows(7)
    window = recommend_posting_window(rows, id_item=1)
    assert window is not None
    assert window.scope == "item-specific"
    assert window.start_hour == 16
    assert window.label == "4 PM–8 PM"
    assert window.confidence == "Low"

    scheduled = next_posting_time(
        window, now=datetime(2026, 1, 8, 18, tzinfo=timezone.utc)
    )
    # 18:00 UTC is 1 PM Eastern, so the next 4 PM window begins at 21:00 UTC.
    assert scheduled == datetime(2026, 1, 8, 21, tzinfo=timezone.utc)


def test_posting_window_labels_short_item_history_as_market_wide_fallback():
    rows = _timing_rows(3, id_item=1) + _timing_rows(3, id_item=2)
    window = recommend_posting_window(rows, id_item=1)
    assert window is not None
    assert window.scope == "market-wide fallback"
    assert window.days_observed == 3


def test_posting_window_does_not_invent_a_best_time_without_any_demand_change():
    rows = _timing_rows(3)
    for row in rows:
        row["negotiations_success"] = 0
        row["negotiations_open"] = 0
    assert recommend_posting_window(rows, id_item=1) is None


def test_inventory_reservation_listing_stock_and_cancellation_are_consistent(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=10,
            id_item=20,
            id_category=30,
            item_name="FS-9 LMG",
            item_slug="fs-9-lmg",
            quantity=10,
            quality=0,
            location="Area18",
            minimum_price=15_000,
        )
        job_ids = await db.create_inventory_post_jobs(
            10,
            [
                {
                    "inventory_id": inventory_id,
                    "quantity": 5,
                    "scheduled_for": datetime.now(timezone.utc) - timedelta(minutes=1),
                }
            ],
        )
        assert len(job_ids) == 1
        inventory = await db.get_inventory_item(10, inventory_id)
        assert inventory["quantity"] == 10
        assert inventory["reserved_quantity"] == 5

        due = await db.list_due_inventory_post_jobs()
        assert [row["id"] for row in due] == job_ids
        assert await db.claim_inventory_post_job(job_ids[0]) is True
        assert await db.claim_inventory_post_job(job_ids[0]) is False
        await db.mark_inventory_post_listed(
            job_ids[0], listing_id=99, listing_url=None, posted_price=16_000, date_expiration=None
        )

        outcome = await db.record_inventory_listing_stock(job_ids[0], in_stock=3, sold_out=False)
        assert outcome == {
            "sold_delta": 2,
            "remaining": 3,
            "status": "listed",
            "inventory_id": inventory_id,
            "user_id": 10,
        }
        inventory = await db.get_inventory_item(10, inventory_id)
        assert inventory["quantity"] == 8
        assert inventory["reserved_quantity"] == 3

        assert await db.cancel_tracked_inventory_listing(10, 99) is True
        inventory = await db.get_inventory_item(10, inventory_id)
        assert inventory["quantity"] == 8
        assert inventory["reserved_quantity"] == 0

    asyncio.run(run())


def test_failed_unambiguous_post_releases_inventory_but_ambiguous_post_does_not(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=1,
            id_item=2,
            id_category=3,
            item_name="Test item",
            item_slug=None,
            quantity=4,
            quality=0,
            location="Orison",
            minimum_price=100,
        )
        first, second = await db.create_inventory_post_jobs(
            1,
            [
                {"inventory_id": inventory_id, "quantity": 2, "scheduled_for": datetime.now(timezone.utc)},
                {"inventory_id": inventory_id, "quantity": 2, "scheduled_for": datetime.now(timezone.utc)},
            ],
        )
        assert await db.claim_inventory_post_job(first)
        await db.mark_inventory_post_failed(first, "definite rejection", ambiguous=False)
        inventory = await db.get_inventory_item(1, inventory_id)
        assert inventory["reserved_quantity"] == 2

        assert await db.claim_inventory_post_job(second)
        await db.mark_inventory_post_failed(second, "response lost", ambiguous=True)
        inventory = await db.get_inventory_item(1, inventory_id)
        assert inventory["reserved_quantity"] == 2
        resolved = await db.confirm_ambiguous_inventory_sale(1, second, 1)
        assert resolved["sold"] == 1
        assert resolved["unsold"] == 1
        inventory = await db.get_inventory_item(1, inventory_id)
        assert inventory["quantity"] == 3
        assert inventory["reserved_quantity"] == 0

    asyncio.run(run())


def test_pending_cancellation_releases_stock_and_interrupted_posts_are_quarantined(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=5,
            id_item=6,
            id_category=7,
            item_name="Quarantine test",
            item_slug=None,
            quantity=6,
            quality=0,
            location="Lorville",
            minimum_price=500,
        )
        pending, interrupted = await db.create_inventory_post_jobs(
            5,
            [
                {"inventory_id": inventory_id, "quantity": 2, "scheduled_for": datetime.now(timezone.utc)},
                {"inventory_id": inventory_id, "quantity": 2, "scheduled_for": datetime.now(timezone.utc)},
            ],
        )
        assert await db.set_inventory_minimum_price(5, inventory_id, 750) is True
        assert (await db.get_inventory_post_job(5, pending))["minimum_price"] == 750
        assert (await db.get_inventory_post_job(5, interrupted))["minimum_price"] == 750
        assert await db.cancel_pending_inventory_post(5, pending) is True
        assert await db.claim_inventory_post_job(interrupted) is True
        async with db.connect() as sqlite:
            await sqlite.execute(
                "UPDATE marketplace_post_jobs SET updated_at = datetime('now', '-1 hour') WHERE id = ?",
                (interrupted,),
            )
            await sqlite.commit()
        stale = await db.flag_stale_inventory_post_jobs(minutes=15)
        assert [row["id"] for row in stale] == [interrupted]
        job = await db.get_inventory_post_job(5, interrupted)
        assert job["status"] == "needs_confirmation"
        inventory = await db.get_inventory_item(5, inventory_id)
        # Cancelled stock was released; ambiguous interrupted stock stays reserved.
        assert inventory["reserved_quantity"] == 2

    asyncio.run(run())


def test_completed_deal_pricing_evidence_is_private_to_inventory_owner(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        listing_ids = {1: 101, 2: 202}
        deal_values = {1: 500.0, 2: 900.0}
        for user_id in (1, 2):
            inventory_id = await db.add_inventory_item(
                user_id=user_id,
                id_item=77,
                id_category=8,
                item_name="Shared catalog item",
                item_slug=None,
                quantity=1,
                quality=0,
                location="Area18",
                minimum_price=100,
            )
            job_id = (await db.create_inventory_post_jobs(
                user_id,
                [{"inventory_id": inventory_id, "quantity": 1, "scheduled_for": datetime.now(timezone.utc)}],
            ))[0]
            assert await db.claim_inventory_post_job(job_id)
            await db.mark_inventory_post_listed(
                job_id,
                listing_id=listing_ids[user_id],
                listing_url=None,
                posted_price=int(deal_values[user_id]),
                date_expiration=None,
            )
            await db.record_inventory_deal_value(
                listing_ids[user_id],
                deal_value=deal_values[user_id],
                currency="UEC",
                date_closed=int(datetime.now(timezone.utc).timestamp()),
            )

        assert await db.get_inventory_completed_unit_prices(
            user_id=1, id_item=77, quality=0, unit="unit"
        ) == [500.0]
        assert await db.get_inventory_completed_unit_prices(
            user_id=2, id_item=77, quality=0, unit="unit"
        ) == [900.0]

    asyncio.run(run())
