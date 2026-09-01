"""Personal inventory pricing, timing, persistence, and posting safety tests."""
from __future__ import annotations

import asyncio
import ast
from datetime import datetime, timedelta, timezone
import inspect

import aiosqlite
from cryptography.fernet import Fernet
import discord
from discord.ext import commands
import httpx

from bot.cogs.personal_inventory import (
    AuthorizeScheduleView,
    CustomPriceModal,
    FloorReachedView,
    InventorySelectionView,
    LowerFloorModal,
    PersonalInventory,
    PostNowView,
    SetMinimumModal,
    SetMinimumPricesView,
)
import bot.cogs.personal_inventory as personal_inventory_module
from bot.db.database import Database
from bot.main import INITIAL_COGS
from bot.uex.client import UexClient
from bot.uex.inventory import (
    build_inventory_listing_payload,
    extract_listing_id,
    recommend_balanced_price,
)
from bot.uex.marketplace import find_item_id_by_name


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "inventory.sqlite3", Fernet(Fernet.generate_key()))


ARLINGTON_CATALOG = [{"id": 8069, "name": "Arlington Rifle", "id_category": 5, "slug": "arlington-rifle"}]


def test_find_item_id_by_name_resolves_a_variant_qualified_query_to_its_base_catalog_item():
    """Confirmed empirically against live UEX listings: there is no separate catalog entry for
    a seller-titled variant like 'Arlington Rifle Widowmaker' - it's the same id_item as the
    plain 'Arlington Rifle'. A query longer/more specific than any exact catalog name should
    still resolve when exactly one catalog name is contained within it."""
    assert find_item_id_by_name(ARLINGTON_CATALOG, "Arlington Rifle Widowmaker") == 8069
    assert find_item_id_by_name(ARLINGTON_CATALOG, 'Arlington "Watchpoint" Rifle') == 8069


def test_find_item_id_by_name_reverse_match_stays_unambiguous():
    catalog = ARLINGTON_CATALOG + [{"id": 1, "name": "Rifle", "id_category": 5, "slug": "rifle"}]
    # Both "Rifle" and "Arlington Rifle" are contained in this query - must not guess.
    assert find_item_id_by_name(catalog, "Arlington Rifle Widowmaker") is None


def test_find_item_id_by_name_exact_and_forward_substring_are_unaffected():
    assert find_item_id_by_name(ARLINGTON_CATALOG, "Arlington Rifle") == 8069
    assert find_item_id_by_name(ARLINGTON_CATALOG, "arlington") == 8069  # forward substring, unique


class _FakeUexCatalog:
    def __init__(self, catalog):
        self._catalog = catalog

    async def get_item_catalog(self):
        return self._catalog


def test_inventory_add_resolves_a_variant_name_and_keeps_it_as_the_displayed_item_name(tmp_path):
    """End-to-end guard for the actual user-facing fix: typing a variant/skin name that isn't
    itself in the catalog must still resolve (via the base item) and must NOT get silently
    collapsed to the catalog's bare name - the variant is what makes the entry meaningful."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = _FakeUexCatalog(ARLINGTON_CATALOG)
        cog = PersonalInventory.__new__(PersonalInventory)
        cog.bot = bot
        interaction = _FakeInteraction(user_id=77)

        await cog.inventory_add.callback(
            cog, interaction, "Arlington Rifle Widowmaker", 1, "Area18",
        )

        (message,), _ = interaction.followup.sent[0]
        assert "Arlington Rifle Widowmaker" in message
        rows = await db.list_inventory(77)
        assert len(rows) == 1
        assert rows[0]["item_name"] == "Arlington Rifle Widowmaker"
        assert rows[0]["id_item"] == 8069

    asyncio.run(run())


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
                "inventory-post-now",
                "inventory-confirm-sale",
                "inventory-cancel-post",
                "inventory-resolve-floor",
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
            {"operation": "sell", "price": "1200", "currency": "UEC", "unit": "unit", "quality": 500, "is_sold_out": 0},
            {"operation": "sell", "price": "1300", "currency": "UEC", "unit": "unit", "quality": 550, "is_sold_out": "0"},
            {"operation": "sell", "price": "1500", "currency": "UEC", "unit": "unit", "quality": 599, "is_sold_out": "1"},
            {"operation": "buy", "price": "1000", "currency": "UEC", "unit": "unit", "quality": 500, "is_sold_out": 0},
            # Wrong quality tier: must not affect a Q500-599 stack.
            {"operation": "sell", "price": "999999", "currency": "UEC", "unit": "unit", "quality": 950, "is_sold_out": 0},
        ],
        average_rows=[
            {
                "quality_tier": 2,
                "operation": "sell",
                "currency": "UEC",
                "unit": "unit",
                "price_avg": "1400",
                "price_avg_week": "1450",
                "price_avg_month": "1500",
            }
        ],
        quality=500,
        unit="unit",
        minimum_price=1425,
    )
    assert recommendation.price == 1425
    assert recommendation.floor_applied is True
    assert "recent sold-out asking prices" in recommendation.evidence
    assert "current competing sell asks" in recommendation.evidence
    assert recommendation.confidence == "High"


def test_balanced_price_excludes_listings_with_unset_or_zero_quality_even_for_a_q0_target():
    # UEX reports both "genuinely Q0" and "seller never set a quality" as the same raw 0
    # (see parse_listing_quality's docstring), so neither can be trusted as confirmed
    # evidence for a Q0 item. Before this fix, `parse_uex_number(...) or 0` treated a
    # missing/zero quality as confirmed tier 0, wrongly matching listings like these.
    recommendation = recommend_balanced_price(
        listings=[
            {"operation": "sell", "price": "50", "currency": "UEC", "unit": "unit", "quality": 0, "is_sold_out": 0},
            {"operation": "sell", "price": "60", "currency": "UEC", "unit": "unit", "quality": None, "is_sold_out": 0},
            {"operation": "buy", "price": "5", "currency": "UEC", "unit": "unit", "quality": 0, "is_sold_out": 0},
        ],
        average_rows=[],
        quality=0,
        unit="unit",
        minimum_price=1000,
    )
    assert recommendation.evidence == ()
    assert recommendation.price == 1000


def test_balanced_price_falls_back_to_manual_floor_when_market_has_no_match():
    recommendation = recommend_balanced_price(
        listings=[], average_rows=[], quality=700, unit="scu", minimum_price=2500
    )
    assert recommendation.price == 2500
    assert recommendation.confidence == "Low"
    assert recommendation.evidence == ()


def test_pricing_strategy_shifts_price_but_never_below_the_manual_floor():
    average_rows = [
        {
            "quality_tier": 0,
            "operation": "sell",
            "currency": "UEC",
            "unit": "unit",
            "price_avg": "1000",
        }
    ]
    balanced = recommend_balanced_price(
        listings=[], average_rows=average_rows, quality=0, unit="unit", minimum_price=1,
    )
    undercut = recommend_balanced_price(
        listings=[], average_rows=average_rows, quality=0, unit="unit", minimum_price=1,
        strategy="undercut",
    )
    premium = recommend_balanced_price(
        listings=[], average_rows=average_rows, quality=0, unit="unit", minimum_price=1,
        strategy="premium",
    )
    assert undercut.price < balanced.price < premium.price

    floored = recommend_balanced_price(
        listings=[], average_rows=average_rows, quality=0, unit="unit", minimum_price=999,
        strategy="undercut",
    )
    assert floored.price == 999
    assert floored.floor_applied is True


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


def test_expire_and_relist_carries_custom_price_forward(tmp_path):
    """Without this, a relisted 'custom' job keeps pricing_strategy='custom' but gets
    custom_price=NULL, and _post_one_job's custom branch does int(job["custom_price"]) -
    a straight crash the next time the background loop tries to post it."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=1, id_item=2, id_category=3, item_name="Custom-priced item",
            item_slug=None, quantity=5, quality=0, location="Orison", minimum_price=1000,
        )
        job_id = (await db.create_inventory_post_jobs(
            1,
            [{
                "inventory_id": inventory_id, "quantity": 5,
                "scheduled_for": datetime.now(timezone.utc),
                "pricing_strategy": "custom", "custom_price": 1500,
            }],
        ))[0]
        assert await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_listed(
            job_id, listing_id=555, listing_url=None, posted_price=1500, date_expiration=None
        )
        await db.record_inventory_listing_stock(job_id, in_stock=3, sold_out=False)

        new_id = await db.expire_and_relist_inventory_post(job_id, datetime.now(timezone.utc))
        assert new_id is not None
        new_job = await db.get_inventory_post_job(1, new_id)
        assert new_job["pricing_strategy"] == "custom"
        assert new_job["custom_price"] == 1500

    asyncio.run(run())


def test_expire_and_relist_with_price_override_uses_custom_pricing(tmp_path):
    """price_override must win regardless of the original strategy - there's no 'recommended
    price' for '5% off what didn't sell', so the replacement always becomes an exact custom
    price rather than trying to recompute a live one."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=1, id_item=2, id_category=3, item_name="Balanced-strategy item",
            item_slug=None, quantity=5, quality=0, location="Orison", minimum_price=1000,
        )
        job_id = (await db.create_inventory_post_jobs(
            1,
            [{"inventory_id": inventory_id, "quantity": 5, "scheduled_for": datetime.now(timezone.utc),
              "pricing_strategy": "balanced"}],
        ))[0]
        assert await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_listed(
            job_id, listing_id=1, listing_url=None, posted_price=2000, date_expiration=None
        )
        await db.record_inventory_listing_stock(job_id, in_stock=3, sold_out=False)

        new_id = await db.expire_and_relist_inventory_post(
            job_id, datetime.now(timezone.utc), price_override=1900
        )
        new_job = await db.get_inventory_post_job(1, new_id)
        assert new_job["pricing_strategy"] == "custom"
        assert new_job["custom_price"] == 1900

    asyncio.run(run())


def test_disable_and_resume_auto_relist(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=1, id_item=2, id_category=3, item_name="Floor test item",
            item_slug=None, quantity=5, quality=0, location="Orison", minimum_price=1000,
        )
        job_id = (await db.create_inventory_post_jobs(
            1,
            [{"inventory_id": inventory_id, "quantity": 5, "scheduled_for": datetime.now(timezone.utc)}],
        ))[0]
        assert await db.claim_inventory_post_job(job_id)
        await db.mark_inventory_post_listed(
            job_id, listing_id=1, listing_url=None, posted_price=1000, date_expiration=None
        )

        await db.disable_auto_relist(job_id)
        assert (await db.get_inventory_post_job(1, job_id))["auto_relist"] == 0

        # Wrong user must not be able to resume someone else's job.
        assert await db.resume_auto_relist_with_new_floor(job_id, 999, 800) is False
        assert await db.resume_auto_relist_with_new_floor(job_id, 1, 800) is True
        job = await db.get_inventory_post_job(1, job_id)
        assert job["auto_relist"] == 1
        assert job["minimum_price"] == 800

    asyncio.run(run())


def test_list_active_inventory_jobs_returns_only_in_progress_statuses(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        inventory_id = await db.add_inventory_item(
            user_id=1, id_item=2, id_category=3, item_name="Multi-job item",
            item_slug=None, quantity=10, quality=0, location="Orison", minimum_price=1000,
        )
        pending_id, listed_id, sold_id = await db.create_inventory_post_jobs(
            1,
            [
                {"inventory_id": inventory_id, "quantity": 2, "scheduled_for": datetime.now(timezone.utc)},
                {"inventory_id": inventory_id, "quantity": 3, "scheduled_for": datetime.now(timezone.utc)},
                {"inventory_id": inventory_id, "quantity": 1, "scheduled_for": datetime.now(timezone.utc)},
            ],
        )
        # pending_id is left untouched (never claimed).
        assert await db.claim_inventory_post_job(listed_id)
        await db.mark_inventory_post_listed(
            listed_id, listing_id=1, listing_url=None, posted_price=1000, date_expiration=None
        )
        assert await db.claim_inventory_post_job(sold_id)
        await db.mark_inventory_post_listed(
            sold_id, listing_id=2, listing_url=None, posted_price=1000, date_expiration=None
        )
        await db.record_inventory_listing_stock(sold_id, in_stock=0, sold_out=True)

        jobs = await db.list_active_inventory_jobs(1)
        statuses = {job["id"]: job["status"] for job in jobs}
        assert statuses == {pending_id: "pending", listed_id: "listed"}  # sold must not appear

    asyncio.run(run())


def test_format_job_status_covers_each_active_state():
    fmt = personal_inventory_module._format_job_status

    pending = fmt({"status": "pending", "scheduled_for": "2026-09-01 15:00:00"})
    assert "Scheduled" in pending and "ET" in pending

    assert fmt({"status": "posting"}) == "Posting to UEX now..."
    assert "inventory-confirm-sale" in fmt({"status": "needs_confirmation"})

    paused = fmt({"status": "listed", "auto_relist": 0})
    assert "paused" in paused.lower()

    fresh_created = (datetime.now(timezone.utc)).isoformat()
    live_fresh = fmt({"status": "listed", "auto_relist": 1, "created_at": fresh_created})
    assert "reprices in ~48h" in live_fresh

    stale_created = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    live_due = fmt({"status": "listed", "auto_relist": 1, "created_at": stale_created})
    assert "due for a reprice check" in live_due


class _FakeResponse:
    def __init__(self):
        self.edits = []
        self.messages = []
        self.sent_modal = None

    async def defer(self, **kwargs):
        pass

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    async def send_modal(self, modal):
        self.sent_modal = modal


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeInteraction:
    def __init__(self, user_id):
        self.user = type("U", (), {"id": user_id})()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def _post_now_client(tmp_path, *, advertise_response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/marketplace_advertise"):
            return httpx.Response(200, json=advertise_response)
        if "marketplace_prices_averages" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "data": []})
        if "marketplace_listings" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "data": []})
        raise AssertionError(f"unexpected request: {request.url}")

    client = UexClient(app_token="test", base_url="https://uex.test")
    return client, handler


async def _setup_post_now(tmp_path, *, advertise_response):
    db = _make_db(tmp_path)
    await db.init()
    user_id = 777
    await db.set_user_secret_key(user_id, "sk_test")
    inventory_id = await db.add_inventory_item(
        user_id=user_id, id_item=55, id_category=9, item_name="Laranite",
        item_slug="laranite", quantity=10, quality=0, location="Area18",
        unit="unit", minimum_price=100,
    )
    entry = await db.get_inventory_item(user_id, inventory_id)

    client, handler = _post_now_client(tmp_path, advertise_response=advertise_response)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    bot = type("FakeBot", (), {})()
    bot.db = db
    bot.uex = client
    cog = PersonalInventory.__new__(PersonalInventory)
    cog.bot = bot
    return db, client, cog, user_id, entry


def test_post_now_confirm_creates_claims_and_posts_the_job_immediately(tmp_path):
    async def run():
        db, client, cog, user_id, entry = await _setup_post_now(
            tmp_path,
            advertise_response={"status": "ok", "data": {"id_listing": 4242, "url": "https://uex.test/l/4242"}},
        )
        try:
            view = PostNowView(cog, user_id, entry, 10)
            interaction = _FakeInteraction(user_id)

            await view.confirm.callback(interaction)

            assert view.resolved is True
            assert all(child.disabled for child in view.children)
            assert len(interaction.followup.sent) == 1
            (message,), _ = interaction.followup.sent[0]
            assert "Posted **[Laranite](https://uexcorp.space/marketplace/home/?id_item=55&mode=list)**" in message
            assert "listing #4242" in message

            jobs = await db.list_tracked_inventory_posts()
            assert len(jobs) == 1
            assert jobs[0]["status"] == "listed"
            assert jobs[0]["listing_id"] == 4242

            inventory_row = await db.get_inventory_item(user_id, int(entry["id"]))
            assert inventory_row["reserved_quantity"] == 10
        finally:
            await client.aclose()

    asyncio.run(run())


def test_post_now_reports_definite_rejection_without_dming_twice(tmp_path):
    async def run():
        db, client, cog, user_id, entry = await _setup_post_now(
            tmp_path,
            advertise_response={"status": "error", "message": "invalid_type", "http_code": 400},
        )
        try:
            view = PostNowView(cog, user_id, entry, 10)
            interaction = _FakeInteraction(user_id)

            dmed = []

            async def _fake_notify(uid, msg):
                dmed.append((uid, msg))

            cog._notify_user = _fake_notify

            await view.confirm.callback(interaction)

            assert dmed == []  # notify=False must suppress the usual DM
            (message,), _ = interaction.followup.sent[0]
            assert "rejected" in message.lower() or "not" in message.lower()

            jobs = await db.list_tracked_inventory_posts()
            assert jobs == []  # failed jobs aren't "listed", so nothing is tracked
        finally:
            await client.aclose()

    asyncio.run(run())


def test_post_now_view_rejects_a_non_owner_interaction(tmp_path):
    async def run():
        db, client, cog, user_id, entry = await _setup_post_now(
            tmp_path, advertise_response={"status": "ok", "data": {"id_listing": 1}}
        )
        try:
            view = PostNowView(cog, user_id, entry, 10)
            interaction = _FakeInteraction(user_id + 1)
            allowed = await view.interaction_check(interaction)
            assert allowed is False
            assert len(interaction.response.messages) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


def test_pricing_strategy_check_migration_preserves_data_and_is_idempotent(tmp_path):
    """A database created before 'custom' existed has pricing_strategy's old 3-value CHECK
    baked into the table - SQLite can't ALTER a CHECK constraint, so this needs a real
    rebuild. This is the highest-risk new code in this change: verify it actually preserves
    existing rows, that a second run doesn't error or duplicate, and that 'custom' really
    is insertable afterward."""
    async def run():
        db_path = tmp_path / "migration.sqlite3"
        # Seed a real personal_inventory row first - list_tracked_inventory_posts INNER
        # JOINs on it, so a job row with no matching inventory row would be silently
        # excluded regardless of the migration, masking what this test is actually checking.
        seed_db = Database(db_path, Fernet(Fernet.generate_key()))
        await seed_db.init()
        inventory_id = await seed_db.add_inventory_item(
            user_id=999, id_item=1, id_category=1, item_name="Gold",
            item_slug="gold", quantity=5, quality=0, location="Area18",
            unit="unit", minimum_price=1000,
        )

        # Now drop back to the pre-'custom' shape by hand: same table, no custom_price
        # column, and the old 3-value CHECK - i.e. exactly what this diff's schema looked
        # like before today - and re-seed one job row against it.
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute("DROP TABLE marketplace_post_jobs")
            await raw.execute(
                """CREATE TABLE marketplace_post_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inventory_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    minimum_price INTEGER NOT NULL CHECK (minimum_price > 0),
                    pricing_strategy TEXT NOT NULL DEFAULT 'balanced' CHECK (
                        pricing_strategy IN ('balanced', 'undercut', 'premium')
                    ),
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (
                        status IN ('pending', 'posting', 'listed', 'expired', 'sold',
                                   'cancelled', 'failed', 'needs_confirmation')
                    ),
                    listing_id INTEGER,
                    listing_url TEXT,
                    posted_price INTEGER,
                    last_known_stock INTEGER,
                    sold_quantity INTEGER NOT NULL DEFAULT 0,
                    deal_value REAL,
                    deal_value_currency TEXT,
                    date_closed INTEGER,
                    date_expiration INTEGER,
                    auto_relist INTEGER NOT NULL DEFAULT 1,
                    relist_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            await raw.execute(
                """INSERT INTO marketplace_post_jobs
                   (id, inventory_id, user_id, quantity, minimum_price, pricing_strategy,
                    scheduled_for, status, listing_id, posted_price)
                   VALUES (5, ?, 999, 3, 1500000, 'premium', '2026-08-27 00:00:00', 'listed', 4242, 1600000)""",
                (inventory_id,),
            )
            await raw.commit()

        db = seed_db
        await db.init()

        rows = await db.list_tracked_inventory_posts()
        assert len(rows) == 1
        assert rows[0]["id"] == 5
        assert rows[0]["pricing_strategy"] == "premium"
        assert rows[0]["listing_id"] == 4242
        assert rows[0]["posted_price"] == 1600000
        assert rows[0]["custom_price"] is None

        # 'custom' must now actually be insertable against the rebuilt table.
        async with db.connect() as conn:
            await conn.execute(
                """INSERT INTO marketplace_post_jobs
                   (inventory_id, user_id, quantity, minimum_price, pricing_strategy,
                    custom_price, scheduled_for, status)
                   VALUES (?, 999, 1, 100, 'custom', 500, '2026-08-27 00:00:00', 'listed')""",
                (inventory_id,),
            )
            await conn.commit()

        # Running init() again (as a normal bot restart would) must be a no-op, not an error.
        await db.init()
        rows_after_second_init = await db.list_tracked_inventory_posts()
        assert len(rows_after_second_init) == 2

    asyncio.run(run())


def test_create_inventory_post_jobs_rejects_custom_price_below_minimum(tmp_path):
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 88
        inventory_id = await db.add_inventory_item(
            user_id=user_id, id_item=1, id_category=1, item_name="Gold",
            item_slug="gold", quantity=5, quality=0, location="Area18",
            unit="unit", minimum_price=1000,
        )
        try:
            await db.create_inventory_post_jobs(
                user_id,
                [{
                    "inventory_id": inventory_id, "quantity": 5, "scheduled_for": datetime.now(timezone.utc),
                    "auto_relist": True, "pricing_strategy": "custom", "custom_price": 500,
                }],
            )
            assert False, "expected a ValueError for a below-minimum custom price"
        except ValueError as exc:
            assert "below" in str(exc).lower()

        # A valid custom price (at or above minimum) must succeed.
        job_ids = await db.create_inventory_post_jobs(
            user_id,
            [{
                "inventory_id": inventory_id, "quantity": 5, "scheduled_for": datetime.now(timezone.utc),
                "auto_relist": True, "pricing_strategy": "custom", "custom_price": 1234,
            }],
        )
        job = await db.get_inventory_post_job(user_id, job_ids[0])
        assert job["pricing_strategy"] == "custom"
        assert job["custom_price"] == 1234

    asyncio.run(run())


def test_post_one_job_uses_the_custom_price_directly_not_the_algorithm(tmp_path):
    async def run():
        db, client, cog, user_id, entry = await _setup_post_now(
            tmp_path,
            advertise_response={"status": "ok", "data": {"id_listing": 777, "url": "https://uex.test/l/777"}},
        )
        try:
            job_ids = await db.create_inventory_post_jobs(
                user_id,
                [{
                    "inventory_id": int(entry["id"]), "quantity": 10, "scheduled_for": datetime.now(timezone.utc),
                    "auto_relist": True, "pricing_strategy": "custom", "custom_price": 250,
                }],
            )
            assert await db.claim_inventory_post_job(job_ids[0])
            job = await db.get_inventory_post_job(user_id, job_ids[0])

            result = await cog._post_one_job(job, notify=False)

            assert result["success"] is True
            assert result["price"] == 250  # exactly what was typed, not a recomputed figure
            assert result["confidence"] == "Custom"
        finally:
            await client.aclose()

    asyncio.run(run())


def test_custom_price_modal_rejects_below_minimum_and_accepts_a_valid_price():
    async def run():
        cog = PersonalInventory.__new__(PersonalInventory)
        entry = {"id": 1, "minimum_price": 1000}
        view = PostNowView(cog, 42, entry, 5)
        modal = CustomPriceModal(view, minimum_price=entry["minimum_price"])
        modal.price_input._value = "500"

        interaction = _FakeInteraction(42)
        await modal.on_submit(interaction)
        assert view.pricing_strategy == "balanced"  # rejected - must not have changed
        assert view.custom_price is None
        (message,), _ = interaction.response.messages[0]
        assert "below your minimum" in message

        modal2 = CustomPriceModal(view, minimum_price=entry["minimum_price"])
        modal2.price_input._value = "1,750,000"
        interaction2 = _FakeInteraction(42)
        await modal2.on_submit(interaction2)
        assert view.pricing_strategy == "custom"
        assert view.custom_price == 1750000
        assert view.choose_pricing_strategy.options[-1].default is True


def test_custom_price_modal_also_works_for_the_batch_authorize_view():
    """CustomPriceModal is shared between PostNowView (single item) and AuthorizeScheduleView
    (batch, gated to one selected stack) - this guards the generalization actually applies to
    both, not just the view it was originally written for."""
    async def run():
        cog = PersonalInventory.__new__(PersonalInventory)
        specs = [{"inventory_id": 1, "quantity": 5, "scheduled_for": datetime.now(timezone.utc),
                   "auto_relist": True, "minimum_price": 1000}]
        view = AuthorizeScheduleView(cog, 42, specs)
        modal = CustomPriceModal(view, minimum_price=specs[0]["minimum_price"])
        modal.price_input._value = "2,500,000"

        interaction = _FakeInteraction(42)
        await modal.on_submit(interaction)

        assert view.pricing_strategy == "custom"
        assert view.custom_price == 2500000

    asyncio.run(run())


def test_authorize_schedule_view_confirm_creates_a_custom_priced_job(tmp_path):
    """The core new-feature guard: confirming a single-stack batch with a custom price must
    thread that exact price into the created job, not silently fall back to a computed one."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 11
        inventory_id = await db.add_inventory_item(
            user_id=user_id, id_item=1, id_category=2, item_name="Arlington Rifle",
            item_slug=None, quantity=1, quality=0, location="Area18", minimum_price=500_000,
        )
        bot = type("FakeBot", (), {})()
        bot.db = db
        cog = PersonalInventory.__new__(PersonalInventory)
        cog.bot = bot

        specs = [{
            "inventory_id": inventory_id, "quantity": 1, "scheduled_for": datetime.now(timezone.utc),
            "auto_relist": True, "minimum_price": 500_000,
        }]
        view = AuthorizeScheduleView(cog, user_id, specs)
        view.pricing_strategy = "custom"
        view.custom_price = 6_833_000
        interaction = _FakeInteraction(user_id)

        await view.confirm.callback(interaction)

        assert view.resolved is True
        jobs = await db.list_active_inventory_jobs(user_id)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "pending"  # AuthorizeScheduleView schedules; a background
        # loop claims and posts it later - "custom" pricing must survive to that later post.
        assert jobs[0]["pricing_strategy"] == "custom"
        assert jobs[0]["custom_price"] == 6_833_000

    asyncio.run(run())


async def _setup_selection_view(tmp_path, *, minimum_prices: dict[str, int | None]):
    """Build a real InventorySelectionView with one inventory row per (item_name, price)
    pair in `minimum_prices`, all pre-selected."""
    db = _make_db(tmp_path)
    await db.init()
    user_id = 55
    ids_by_name: dict[str, int] = {}
    for item_name, minimum_price in minimum_prices.items():
        inventory_id = await db.add_inventory_item(
            user_id=user_id, id_item=hash(item_name) % 1000, id_category=9, item_name=item_name,
            item_slug=item_name.lower(), quantity=10, quality=0, location="Area18",
            unit="unit", minimum_price=minimum_price,
        )
        ids_by_name[item_name] = inventory_id

    bot = type("FakeBot", (), {})()
    bot.db = db
    cog = PersonalInventory.__new__(PersonalInventory)
    cog.bot = bot

    rows = await db.list_inventory(user_id)
    view = InventorySelectionView(cog, user_id, rows)
    view.selected_ids = set(ids_by_name.values())
    return db, cog, user_id, view, ids_by_name


def test_review_selected_offers_inline_minimum_buttons_when_a_floor_is_missing(tmp_path):
    """The old behavior dead-ended with a text pointer to /inventory-set-minimum, making the
    user re-run /inventory-sell from scratch after leaving to fix it. It must now offer a
    way to set the floor without leaving the flow."""
    async def run():
        db, cog, user_id, view, ids_by_name = await _setup_selection_view(
            tmp_path, minimum_prices={"Laranite": None}
        )
        interaction = _FakeInteraction(user_id)

        await view.review_selected(interaction)

        (_,), kwargs = interaction.followup.sent[0]
        assert isinstance(kwargs.get("view"), SetMinimumPricesView)
        assert kwargs.get("ephemeral") is True

    asyncio.run(run())


def test_setting_the_only_missing_minimum_continues_straight_into_the_authorize_screen(tmp_path):
    """The core new behavior: once the last missing floor is set, the SAME message must
    turn into the authorize screen - not a dead end, not a second /inventory-sell run."""
    async def run():
        db, cog, user_id, view, ids_by_name = await _setup_selection_view(
            tmp_path, minimum_prices={"Laranite": None}
        )
        interaction = _FakeInteraction(user_id)
        await view.review_selected(interaction)
        (_,), kwargs = interaction.followup.sent[0]
        prices_view: SetMinimumPricesView = kwargs["view"]

        button = next(c for c in prices_view.children if "Laranite" in (c.label or ""))
        button_interaction = _FakeInteraction(user_id)
        await button.callback(button_interaction)
        modal = button_interaction.response.sent_modal
        assert isinstance(modal, SetMinimumModal)

        modal.price_input._value = "500,000"
        modal_interaction = _FakeInteraction(user_id)
        await modal.on_submit(modal_interaction)

        entry = await db.get_inventory_item(user_id, ids_by_name["Laranite"])
        assert entry["minimum_price"] == 500_000

        (edit,) = modal_interaction.response.edits
        assert isinstance(edit.get("view"), AuthorizeScheduleView)
        assert edit.get("embed") is not None

    asyncio.run(run())


def test_setting_one_of_two_missing_minimums_leaves_the_other_button_showing(tmp_path):
    async def run():
        db, cog, user_id, view, ids_by_name = await _setup_selection_view(
            tmp_path, minimum_prices={"Laranite": None, "Gold": None}
        )
        interaction = _FakeInteraction(user_id)
        await view.review_selected(interaction)
        (_,), kwargs = interaction.followup.sent[0]
        prices_view: SetMinimumPricesView = kwargs["view"]

        laranite_button = next(c for c in prices_view.children if "Laranite" in (c.label or ""))
        button_interaction = _FakeInteraction(user_id)
        await laranite_button.callback(button_interaction)
        modal = button_interaction.response.sent_modal
        modal.price_input._value = "100000"
        modal_interaction = _FakeInteraction(user_id)
        await modal.on_submit(modal_interaction)

        # Still one floor missing - must show the remaining button, not jump to authorize.
        (edit,) = modal_interaction.response.edits
        assert isinstance(edit.get("view"), SetMinimumPricesView)
        remaining_labels = [c.label for c in edit["view"].children]
        assert any("Gold" in label for label in remaining_labels)
        assert not any("Laranite" in label for label in remaining_labels)

        laranite_entry = await db.get_inventory_item(user_id, ids_by_name["Laranite"])
        assert laranite_entry["minimum_price"] == 100_000

    asyncio.run(run())


def test_post_now_reports_a_friendly_error_when_live_pricing_fails(tmp_path):
    """Without a try/except here, a transient UEX failure while building the preview
    propagates out of the command callback uncaught instead of a normal ephemeral reply."""
    async def run():
        db = _make_db(tmp_path)
        await db.init()
        user_id = 888
        await db.set_user_secret_key(user_id, "sk_test")
        inventory_id = await db.add_inventory_item(
            user_id=user_id, id_item=55, id_category=9, item_name="Laranite",
            item_slug="laranite", quantity=10, quality=0, location="Area18",
            unit="unit", minimum_price=100,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "marketplace_listings" in request.url.path:
                return httpx.Response(500, json={"status": "error", "message": "boom", "http_code": 500})
            if "marketplace_prices_averages" in request.url.path:
                return httpx.Response(200, json={"status": "ok", "data": []})
            raise AssertionError(f"unexpected request: {request.url}")

        client = UexClient(app_token="test", base_url="https://uex.test")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        bot = type("FakeBot", (), {})()
        bot.db = db
        bot.uex = client
        cog = PersonalInventory.__new__(PersonalInventory)
        cog.bot = bot
        interaction = _FakeInteraction(user_id)

        try:
            await cog.inventory_post_now.callback(cog, interaction, inventory_id)

            assert interaction.followup.sent, "should reply with a friendly error, not raise"
            (message,), kwargs = interaction.followup.sent[0]
            assert "could not price" in message.lower()
            assert kwargs.get("ephemeral") is True
        finally:
            await client.aclose()

    asyncio.run(run())


async def _setup_reconcile(
    tmp_path, *, hours_old, minimum_price=850_000, posted_price=1_000_000,
    negotiation_rows=None, negotiations_fail=False, advertise_fails=False,
):
    db = _make_db(tmp_path)
    await db.init()
    user_id = 777
    await db.set_user_secret_key(user_id, "sk_test")
    inventory_id = await db.add_inventory_item(
        user_id=user_id, id_item=55, id_category=9, item_name="Laranite",
        item_slug="laranite", quantity=10, quality=0, location="Area18",
        unit="unit", minimum_price=minimum_price,
    )
    job_id = (await db.create_inventory_post_jobs(
        user_id,
        [{"inventory_id": inventory_id, "quantity": 10, "scheduled_for": datetime.now(timezone.utc)}],
    ))[0]
    assert await db.claim_inventory_post_job(job_id)
    await db.mark_inventory_post_listed(
        job_id, listing_id=555, listing_url=None, posted_price=posted_price, date_expiration=None
    )
    async with db.connect() as sqlite:
        await sqlite.execute(
            "UPDATE marketplace_post_jobs SET created_at = datetime('now', ?) WHERE id = ?",
            (f"-{hours_old} hours", job_id),
        )
        await sqlite.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and "marketplace_listings" in request.url.path:
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("/marketplace_advertise"):
            if advertise_fails:
                return httpx.Response(400, json={"status": "error", "message": "invalid_type", "http_code": 400})
            return httpx.Response(200, json={"status": "ok", "data": {"id_listing": 556, "url": "https://uex.test/l/556"}})
        if "marketplace_prices_averages" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "data": []})
        if "marketplace_listings" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "data": [
                {"id": 555, "in_stock": 10, "is_sold_out": False},
            ]})
        if "marketplace_negotiations" in request.url.path:
            if negotiations_fail:
                return httpx.Response(500, json={"status": "error", "message": "boom", "http_code": 500})
            return httpx.Response(200, json={"status": "ok", "data": negotiation_rows or []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = UexClient(app_token="test", base_url="https://uex.test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    bot = type("FakeBot", (), {})()
    bot.db = db
    bot.uex = client
    cog = PersonalInventory.__new__(PersonalInventory)
    cog.bot = bot

    dmed: list[tuple[int, str]] = []

    async def _fake_notify(uid, msg):
        dmed.append((uid, msg))

    cog._notify_user = _fake_notify

    return db, client, cog, user_id, job_id, dmed


def test_reconcile_discounts_an_unsold_listing_after_48_hours(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=1_000_000,
        )
        try:
            await cog._reconcile_listed_jobs()

            old_job = await db.get_inventory_post_job(user_id, job_id)
            assert old_job["status"] == "expired"

            jobs = await db.list_tracked_inventory_posts()
            assert len(jobs) == 1
            new_job = jobs[0]
            assert new_job["pricing_strategy"] == "custom"
            assert new_job["custom_price"] == 950_000  # 1,000,000 * 0.95
            assert new_job["auto_relist"] == 1
            assert len(dmed) == 1
            assert "950,000" in dmed[0][1]
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_clamps_the_discount_at_the_minimum_price(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=857_375,
        )
        try:
            await cog._reconcile_listed_jobs()
            jobs = await db.list_tracked_inventory_posts()
            assert jobs[0]["custom_price"] == 850_000  # would-be 814,506 clamped to the floor
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_stops_and_prompts_at_the_floor_with_no_interest(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=850_000,
        )
        sent_views = []

        class _FakeDiscordUser:
            async def send(self, *args, **kwargs):
                sent_views.append((args, kwargs))

        cog.bot.get_user = lambda uid: _FakeDiscordUser()
        try:
            await cog._reconcile_listed_jobs()

            job = await db.get_inventory_post_job(user_id, job_id)
            assert job["status"] == "listed"  # never touched - already at the floor
            assert job["auto_relist"] == 0
            assert dmed == []  # this path uses the interactive DM, not plain _notify_user
            assert len(sent_views) == 1
            _, kwargs = sent_views[0]
            assert "view" in kwargs
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_pauses_without_discounting_when_a_negotiation_is_open(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=1_000_000,
            negotiation_rows=[{
                "id": 9, "id_listing": 555, "date_modified": 100, "date_closed": None,
                "listing_title": "Laranite",
            }],
        )
        try:
            await cog._reconcile_listed_jobs()

            job = await db.get_inventory_post_job(user_id, job_id)
            assert job["status"] == "listed"
            assert job["posted_price"] == 1_000_000  # untouched, not discounted
            assert job["auto_relist"] == 0
            assert len(dmed) == 1
            assert "negotiation" in dmed[0][1].lower()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_does_not_reprice_when_negotiation_fetch_fails(tmp_path):
    """A failed negotiation fetch must never be treated as 'verified: no negotiation' -
    that would let the bot reprice/relist blind while a negotiation might genuinely be
    open. It should retry next cycle instead, silently (matching how the other transient
    failure in this same function - the delete-listing UexApiError - already behaves)."""
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=1_000_000,
            negotiations_fail=True,
        )
        try:
            await cog._reconcile_listed_jobs()

            job = await db.get_inventory_post_job(user_id, job_id)
            assert job["status"] == "listed"
            assert job["posted_price"] == 1_000_000  # untouched
            assert job["auto_relist"] == 1  # not disabled - this should retry, not give up
            assert dmed == []
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_detects_an_open_negotiation_even_when_an_older_one_is_closed(tmp_path):
    """Regression guard: picking a single 'best' negotiation per listing by (closed, then
    date_modified) always ranks any closed negotiation above any open one in tuple
    comparison, regardless of which is actually more recent - so an older closed
    negotiation could hide a genuinely newer open one for the same listing. Open-listing
    detection must not depend on that 'best' pick."""
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=1_000_000,
            negotiation_rows=[
                {"id": 1, "id_listing": 555, "date_modified": 100, "date_closed": 200, "listing_title": "Laranite"},
                {"id": 2, "id_listing": 555, "date_modified": 500, "date_closed": None, "listing_title": "Laranite"},
            ],
        )
        try:
            await cog._reconcile_listed_jobs()

            job = await db.get_inventory_post_job(user_id, job_id)
            assert job["status"] == "listed"
            assert job["posted_price"] == 1_000_000  # untouched, not discounted
            assert job["auto_relist"] == 0
            assert len(dmed) == 1
            assert "negotiation" in dmed[0][1].lower()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_reports_failure_honestly_when_the_relist_post_does_not_succeed(tmp_path):
    """The old listing is deleted before the replacement is posted, so if the new post
    fails, the item has NO active listing - the bot must say so, never claim success."""
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=1_000_000,
            advertise_fails=True,
        )
        try:
            await cog._reconcile_listed_jobs()

            assert len(dmed) == 1
            message = dmed[0][1].lower()
            assert "relisted as job" not in message
            assert "no active listing" in message
        finally:
            await client.aclose()

    asyncio.run(run())


def test_reconcile_does_nothing_before_48_hours(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=1, minimum_price=850_000, posted_price=1_000_000,
        )
        try:
            await cog._reconcile_listed_jobs()

            job = await db.get_inventory_post_job(user_id, job_id)
            assert job["status"] == "listed"
            assert job["auto_relist"] == 1
            assert dmed == []
            assert len(await db.list_tracked_inventory_posts()) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


def test_lower_floor_modal_rejects_invalid_input_and_resumes_on_success(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=850_000,
        )
        try:
            await db.disable_auto_relist(job_id)
            job = await db.get_inventory_post_job(user_id, job_id)
            view = FloorReachedView(cog, job)
            modal = LowerFloorModal(view)
            modal.price_input._value = "not a number"

            interaction = _FakeInteraction(user_id)
            await modal.on_submit(interaction)
            (message,), _ = interaction.response.messages[0]
            assert "not a whole number" in message

            modal2 = LowerFloorModal(view)
            modal2.price_input._value = "700,000"
            interaction2 = _FakeInteraction(user_id)
            await modal2.on_submit(interaction2)
            assert interaction2.response.edits, "should edit the original DM message, not send a new one"

            updated = await db.get_inventory_post_job(user_id, job_id)
            assert updated["minimum_price"] == 700_000
            assert updated["auto_relist"] == 1
        finally:
            await client.aclose()

    asyncio.run(run())


def test_resolve_floor_command_resends_a_working_prompt_for_a_paused_job(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=49, minimum_price=850_000, posted_price=850_000,
        )
        try:
            await db.disable_auto_relist(job_id)
            interaction = _FakeInteraction(user_id)

            await cog.inventory_resolve_floor.callback(cog, interaction, job_id)

            _, kwargs = interaction.response.messages[0]
            assert kwargs.get("ephemeral") is True
            assert isinstance(kwargs.get("view"), FloorReachedView)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_resolve_floor_command_rejects_a_job_that_is_not_paused(tmp_path):
    async def run():
        db, client, cog, user_id, job_id, dmed = await _setup_reconcile(
            tmp_path, hours_old=1, minimum_price=850_000, posted_price=1_000_000,
        )
        try:
            interaction = _FakeInteraction(user_id)

            await cog.inventory_resolve_floor.callback(cog, interaction, job_id)

            (message,), kwargs = interaction.response.messages[0]
            assert "isn't currently paused" in message
            assert kwargs.get("ephemeral") is True
        finally:
            await client.aclose()

    asyncio.run(run())

    asyncio.run(run())
