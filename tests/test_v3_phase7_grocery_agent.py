"""Tests for V3 Phase 7: Messa Autonomous Groceries & Lifestyle Concierge (grocery_agent).
Covers:
  1. Database operations (user_dietary_profiles, user_pantry_items, grocery_orders)
     and graceful degradation pre-migration-042.
  2. Dietary profile & pantry tools (get/update profile, list/add/remove staples).
  3. Recipe & meal plan generator (Spoonacular API with automatic LLM fallback).
  4. Multi-modal fridge vision inventory analysis (waste minimization & restock detection).
  5. Instacart 1-Tap Cart Staging & Express Checkout URL generation (no Composio, direct links).
  6. Restaurant takeout & delivery staging via DoorDash / Uber Eats deep links.
  7. Autonomous Pantry Restock Autopilot hooked into Phase 6 Project Capsules.
  8. Package return concierge & courier logistics with Paid-tier gating (Basic vs Pro/Plus/Admin).
  9. Orchestrator registration, prompt routing, and GROCERY_AGENT_ENABLED kill switch.

No live Postgres, no live paid LLM calls.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

import types as _types
_fake_composio_exceptions = _types.ModuleType("composio.exceptions")


class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = _types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402

from messa import config, db  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import grocery_tools  # noqa: E402

failures = []


def check(label: str, cond: bool):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


USER = config.UserContext(
    user_id=1, phone_number="+15551234567", name="Test User",
    timezone="America/New_York", onboarding_step="complete", plan_id="basic",
)
PAID_USER = config.UserContext(
    user_id=2, phone_number="+15559876543", name="Pro User",
    timezone="America/New_York", onboarding_step="complete", plan_id="pro",
)
ADMIN_USER = config.UserContext(
    user_id=3, phone_number="+15550001111", name="Admin User",
    timezone="America/New_York", onboarding_step="complete", plan_id="basic", is_admin=True,
)


class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_queue=None, fetchval_queue=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_queue = list(fetch_queue or [])
        self.fetchval_queue = list(fetchval_queue or [])
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "information_schema" in query:
            return self.has_tables
        if self.fetchval_queue:
            return self.fetchval_queue.pop(0)
        return self.has_tables

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if self.fetch_queue:
            return self.fetch_queue.pop(0)
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 1"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeRow(dict):
    pass


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


# ---------------------------------------------------------------------------
# Part 1: DB Operations & Pre-Migration Degrades
# ---------------------------------------------------------------------------

async def part1_db_operations():
    # 1a: Pre-migration graceful degrades
    no_table_conn = FakeConn(has_tables=False)
    install_fake_pool(no_table_conn)

    prof = await db.get_user_dietary_profile(1)
    check("db.get_user_dietary_profile: None when table missing", prof is None)

    upsert_prof = await db.upsert_user_dietary_profile(1, dietary_flags=["gluten-free"])
    check("db.upsert_user_dietary_profile: None when table missing", upsert_prof is None)

    pantry = await db.list_pantry_items(1)
    check("db.list_pantry_items: [] when table missing", pantry == [])

    upsert_p = await db.upsert_pantry_item(1, item_name="Milk")
    check("db.upsert_pantry_item: None when table missing", upsert_p is None)

    rem_p = await db.remove_pantry_item(1, "Milk")
    check("db.remove_pantry_item: False when table missing", rem_p is False)

    orders = await db.list_grocery_orders(1)
    check("db.list_grocery_orders: [] when table missing", orders == [])

    ord_create = await db.create_grocery_order(1, cart_provider="instacart", store_name="Whole Foods", items_count=2, estimated_total=10.0, checkout_url="https://...")
    check("db.create_grocery_order: None when table missing", ord_create is None)

    # 1b: Success round-trips with tables present
    profile_row = FakeRow(
        user_id=1, dietary_flags=["gluten-free", "high-protein"], allergies=["peanuts"],
        disliked_ingredients=["cilantro"], household_size=2, preferred_store="sprouts",
    )
    conn = FakeConn(has_tables=True, fetchrow_queue=[profile_row])
    install_fake_pool(conn)
    res = await db.get_user_dietary_profile(1)
    check("db.get_user_dietary_profile: returns dict", res == profile_row)

    # 1c: Pantry item list and upsert
    pantry_rows = [
        FakeRow(id=10, user_id=1, item_name="Oat Milk", category="dairy", preferred_brand="Oatly", restock_frequency_days=7),
        FakeRow(id=11, user_id=1, item_name="Eggs", category="dairy", preferred_brand="Vital Farms", restock_frequency_days=7),
    ]
    conn2 = FakeConn(has_tables=True, fetch_queue=[pantry_rows])
    install_fake_pool(conn2)
    p_res = await db.list_pantry_items(1)
    check("db.list_pantry_items: returns pantry rows", len(p_res) == 2 and p_res[0]["item_name"] == "Oat Milk")

    # 1d: Grocery orders
    order_row = FakeRow(
        id=99, user_id=1, cart_provider="instacart", store_name="Whole Foods Market",
        items_count=3, estimated_total=16.50, checkout_url="https://instacart.com/...", status="staged",
    )
    conn3 = FakeConn(has_tables=True, fetchrow_queue=[order_row], fetch_queue=[[order_row]])
    install_fake_pool(conn3)
    created = await db.create_grocery_order(1, cart_provider="instacart", store_name="Whole Foods Market", items_count=3, estimated_total=16.50, checkout_url="https://instacart.com/...")
    check("db.create_grocery_order: returns inserted row", created["id"] == 99)
    listed = await db.list_grocery_orders(1)
    check("db.list_grocery_orders: returns list", len(listed) == 1 and listed[0]["cart_provider"] == "instacart")


# ---------------------------------------------------------------------------
# Part 2: Dietary Profile & Pantry Tools
# ---------------------------------------------------------------------------

async def part2_dietary_and_pantry_tools():
    tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}

    # 2a: get_dietary_profile tool
    profile_row = FakeRow(
        user_id=1, dietary_flags=["keto", "dairy-free"], allergies=["shellfish"],
        disliked_ingredients=["mushrooms"], household_size=3, preferred_store="whole_foods",
    )
    pantry_rows = [
        FakeRow(id=1, user_id=1, item_name="Avocado Oil", category="staple", preferred_brand="Chosen Foods", restock_frequency_days=14),
    ]
    conn = FakeConn(has_tables=True, fetchrow_queue=[profile_row], fetch_queue=[pantry_rows])
    install_fake_pool(conn)

    out = await tools_map["get_dietary_profile"].ainvoke({})
    check("get_dietary_profile: mentions dietary flags", "keto" in out and "dairy-free" in out)
    check("get_dietary_profile: mentions allergies", "shellfish" in out)
    check("get_dietary_profile: mentions pantry staple", "Avocado Oil" in out)

    # 2b: update_dietary_profile tool
    updated_row = FakeRow(
        user_id=1, dietary_flags=["high-protein"], allergies=["peanuts"],
        disliked_ingredients=["cilantro"], household_size=1, preferred_store="aldi",
    )
    # 1st fetchrow is existing check, 2nd fetchrow is RETURNING row from update
    conn2 = FakeConn(has_tables=True, fetchrow_queue=[profile_row, updated_row])
    install_fake_pool(conn2)

    up_out = await tools_map["update_dietary_profile"].ainvoke({
        "dietary_flags": ["high-protein"],
        "allergies": "peanuts",
        "preferred_store": "aldi",
    })
    check("update_dietary_profile: confirms updated store ALDI", "ALDI" in up_out)
    check("update_dietary_profile: confirms allergies", "peanuts" in up_out)

    # 2c: pantry staple tools
    conn3 = FakeConn(has_tables=True, fetchrow_queue=[None, FakeRow(id=1, item_name="Coffee Beans", category="staple", preferred_brand="Stumptown", restock_frequency_days=7)])
    install_fake_pool(conn3)
    add_out = await tools_map["add_pantry_staple"].ainvoke({
        "item_name": "Coffee Beans", "preferred_brand": "Stumptown", "category": "staple",
    })
    check("add_pantry_staple: confirms saved staple", "Saved staple 'Coffee Beans'" in add_out)


# ---------------------------------------------------------------------------
# Part 3: Recipe & Meal Plan Generator (Spoonacular + LLM Fallback)
# ---------------------------------------------------------------------------

async def part3_meal_plan_generator():
    tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}

    # 3a: LLM Fallback when Spoonacular key is empty or error
    config.SPOONACULAR_API_KEY = ""
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value = AIMessage(
        content="=== 3-Day High-Protein Meal Plan ===\nDay 1: Grilled Chicken & Quinoa\nDay 2: Salmon & Asparagus\nDay 3: Turkey Chili\n\nShopping List:\n- 2 lbs chicken breast\n- 1 lb salmon\n- 1 bunch asparagus"
    )

    tools_with_mock_llm = {t.name: t for t in grocery_tools.build_grocery_tools(USER, model=mock_llm)}
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(user_id=1, dietary_flags=["high-protein"], allergies=[], disliked_ingredients=[], household_size=1, preferred_store="whole_foods")])
    install_fake_pool(conn)

    plan_out = await tools_with_mock_llm["generate_meal_plan"].ainvoke({"days": 3, "cuisine": "Mediterranean"})
    check("generate_meal_plan (LLM fallback): invokes model", mock_llm.ainvoke.called)
    check("generate_meal_plan (LLM fallback): returns meal plan content", "Grilled Chicken" in plan_out and "Shopping List" in plan_out)

    # 3b: Spoonacular API success branch
    config.SPOONACULAR_API_KEY = "dummy-spoonacular-key"
    fake_spoonacular_resp = {
        "meals": [
            {"id": 1, "title": "Avocado Toast with Poached Eggs", "readyInMinutes": 15, "servings": 1},
            {"id": 2, "title": "Lemon Herb Roast Chicken", "readyInMinutes": 45, "servings": 2},
        ],
        "nutrients": {"calories": 1850.0, "protein": 140.0, "carbohydrates": 120.0, "fat": 65.0},
    }

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_spoonacular_resp

    with patch("httpx.AsyncClient.get", return_value=mock_resp):
        spoon_out = await tools_map["generate_meal_plan"].ainvoke({"days": 1})
        check("generate_meal_plan (Spoonacular): formats spoonacular meals", "Avocado Toast" in spoon_out and "Lemon Herb Roast Chicken" in spoon_out)
        check("generate_meal_plan (Spoonacular): formats daily nutrients", "1850" in spoon_out and "140" in spoon_out)

    config.SPOONACULAR_API_KEY = ""


# ---------------------------------------------------------------------------
# Part 4: Fridge Vision Inventory Tool
# ---------------------------------------------------------------------------

async def part4_fridge_vision_inventory():
    mock_vision_model = AsyncMock()
    mock_vision_model.ainvoke.return_value = AIMessage(
        content="Detected in Fridge:\n- 1 carton organic eggs (half full)\n- Greek yogurt\n- Spinach (use within 2 days)\n- Cheddar cheese\n\nMeal Idea:\nQuick spinach & cheddar omelet."
    )

    with patch("messa.config.build_model", return_value=mock_vision_model):
        tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}
        out = await tools_map["analyze_fridge_inventory"].ainvoke({
            "image_urls": ["https://cdn.sendblue.co/media/fridge_photo.jpg"],
            "notes": "Milk is almost out",
        })
        check("analyze_fridge_inventory: calls vision model", mock_vision_model.ainvoke.called)
        check("analyze_fridge_inventory: returns vision model output", "spinach & cheddar omelet" in out)

        # Check call arguments contain image_url block
        call_args = mock_vision_model.ainvoke.call_args[0][0]
        content_blocks = call_args[0].content
        has_img = any(isinstance(b, dict) and b.get("type") == "image_url" for b in content_blocks)
        check("analyze_fridge_inventory: passes image_url block to model", has_img)


# ---------------------------------------------------------------------------
# Part 5: Instacart 1-Tap Cart Staging & Express Checkout
# ---------------------------------------------------------------------------

async def part5_instacart_staging():
    tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}

    # 5a: Guest Mode (Default Out-of-the-box, no connected account)
    profile_row = FakeRow(user_id=1, preferred_store="whole_foods")
    pantry_rows = [
        FakeRow(id=1, user_id=1, item_name="eggs", preferred_brand="Vital Farms"),
        FakeRow(id=2, user_id=1, item_name="milk", preferred_brand="Oatly Full Fat"),
    ]
    order_row = FakeRow(id=101, user_id=1, cart_provider="instacart", store_name="Whole Foods Market", items_count=3, estimated_total=16.50, checkout_url="https://...", status="staged")

    # fetchrow for get_user_dietary_profile, then get_active_app_connection (returns None for guest), then create_grocery_order
    conn = FakeConn(has_tables=True, fetchrow_queue=[profile_row, None, order_row], fetch_queue=[pantry_rows])
    install_fake_pool(conn)

    cart_out = await tools_map["stage_instacart_cart"].ainvoke({
        "store_name": "Whole Foods",
        "items_list": ["eggs", "milk", "organic avocados"],
    })

    check("stage_instacart_cart (guest): enriches with preferred brand Vital Farms", "Vital Farms eggs" in cart_out)
    check("stage_instacart_cart (guest): enriches with preferred brand Oatly", "Oatly Full Fat milk" in cart_out)
    check("stage_instacart_cart (guest): includes 1-tap express link", "[Review Cart & Order with Apple Pay ➔]" in cart_out)
    check("stage_instacart_cart (guest): includes retailer slug in checkout url", "retailer=whole-foods" in cart_out)
    check("stage_instacart_cart (guest): mentions 2-second Apple Pay checkout", "Apple Pay / FaceID" in cart_out)
    check("stage_instacart_cart (guest): includes optional connect tip", "Connect Instacart" in cart_out)

    # 5b: Connected Mode (User connected personal account via Composio)
    active_conn_row = FakeRow(id=88, user_id=1, app_name="instacart", connected_account_id="acc_12345", is_active=True)
    conn2 = FakeConn(has_tables=True, fetchrow_queue=[profile_row, active_conn_row, order_row], fetch_queue=[pantry_rows])
    install_fake_pool(conn2)

    mock_composio_client = MagicMock()
    mock_composio_client.tools.execute.return_value = {
        "status": "SUCCESS",
        "data": {"url": "https://www.instacart.com/store/lists/messa-synced-list-999"},
    }

    with patch("messa.tools.integration_tools._get_client", return_value=mock_composio_client), \
         patch("messa.config.COMPOSIO_API_KEY", "comp_test_key"):
        connected_cart = await tools_map["stage_instacart_cart"].ainvoke({
            "store_name": "Whole Foods",
            "items_list": ["eggs", "milk"],
        })
        check("stage_instacart_cart (connected): formats connected account header", "Cart Synced to Your Connected Account" in connected_cart)
        check("stage_instacart_cart (connected): includes personal account URL", "https://www.instacart.com/store/lists/messa-synced-list-999" in connected_cart)

    # 5c: connect_instacart and disconnect_instacart tools
    # When not connected, calls send_connect_link
    conn3 = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn3)
    with patch("messa.tools.integration_tools.send_connect_link", new_callable=AsyncMock) as mock_send_link:
        mock_send_link.return_value = "Sent you a secure link to connect your Instacart account!"
        conn_res = await tools_map["connect_instacart"].ainvoke({})
        check("connect_instacart: invokes send_connect_link when not connected", mock_send_link.called)
        check("connect_instacart: returns connection prompt", "secure link to connect" in conn_res)

    # When already connected
    conn4 = FakeConn(has_tables=True, fetchrow_queue=[active_conn_row])
    install_fake_pool(conn4)
    conn_already = await tools_map["connect_instacart"].ainvoke({})
    check("connect_instacart: informs already connected", "already connected" in conn_already)

    # disconnect_instacart
    conn5 = FakeConn(has_tables=True, fetchrow_queue=[active_conn_row])
    install_fake_pool(conn5)
    with patch("messa.tools.integration_tools._get_client", return_value=mock_composio_client):
        disconn_res = await tools_map["disconnect_instacart"].ainvoke({})
        check("disconnect_instacart: disconnects and confirms guest mode", "Reverted to default Guest 1-Tap" in disconn_res)

    # 5d: get_nearby_grocery_stores
    conn6 = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn6)
    nearby_res = await tools_map["get_nearby_grocery_stores"].ainvoke({"postal_code": "33101"})
    check("get_nearby_grocery_stores: returns retailers for zip", "Zip 33101" in nearby_res and "Whole Foods Market" in nearby_res)


# ---------------------------------------------------------------------------
# Part 6: Restaurant Takeout & Errand Staging (DoorDash / Uber Eats)
# ---------------------------------------------------------------------------

async def part6_restaurant_order_staging():
    tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}

    order_row = FakeRow(id=202, user_id=1, cart_provider="doordash", store_name="Chipotle", items_count=2, estimated_total=33.00, checkout_url="https://...", status="staged")
    conn = FakeConn(has_tables=True, fetchrow_queue=[order_row])
    install_fake_pool(conn)

    takeout_out = await tools_map["stage_restaurant_order"].ainvoke({
        "restaurant_name": "Chipotle",
        "items": ["Chicken Burrito Bowl", "Chips & Guacamole"],
        "order_type": "pickup",
    })

    check("stage_restaurant_order: formats restaurant title", "Chipotle (Pickup Order)" in takeout_out)
    check("stage_restaurant_order: includes DoorDash link with pickup param", "doordash.com/search/store/Chipotle/?pickup=true" in takeout_out)
    check("stage_restaurant_order: includes Uber Eats link", "ubereats.com/search?q=Chipotle&orderType=pickup" in takeout_out)


# ---------------------------------------------------------------------------
# Part 7: Autonomous Pantry Restock Capsule (Phase 6 Integration)
# ---------------------------------------------------------------------------

async def part7_pantry_restock_capsule():
    tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}

    pending_row = FakeRow(id=777, action_type="create_project", payload={})
    pantry_rows = [
        FakeRow(id=1, user_id=1, item_name="coffee beans"),
        FakeRow(id=2, user_id=1, item_name="eggs"),
    ]
    # fetch_queue for list_pantry_items, fetchrow_queue for propose_action
    conn = FakeConn(has_tables=True, fetch_queue=[pantry_rows], fetchrow_queue=[pending_row])
    install_fake_pool(conn)

    capsule_out = await tools_map["schedule_pantry_restock_capsule"].ainvoke({
        "cadence": "weekly",
        "essentials_list": ["coffee beans", "oat milk", "organic eggs"],
    })

    check("schedule_pantry_restock_capsule: stages pending action", "Proposed (pending confirmation, id #777)" in capsule_out)
    check("schedule_pantry_restock_capsule: mentions weekly Friday checkin", "weekly every Friday at 2:00 PM" in capsule_out)
    check("schedule_pantry_restock_capsule: mentions 1-tap Instacart link", "1-tap Instacart Apple Pay link" in capsule_out)


# ---------------------------------------------------------------------------
# Part 8: Package Return Concierge & Paid Tier Gating
# ---------------------------------------------------------------------------

async def part8_package_return_gating():
    # 8a: Free/Basic user is gated
    basic_tools = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}
    free_out = await basic_tools["track_package_and_schedule_return"].ainvoke({
        "tracking_number": "1Z9999999999999999",
        "carrier": "auto",
    })
    check("track_package_and_schedule_return: blocks Basic tier", "exclusive feature of Messa Paid Plans" in free_out)
    check("track_package_and_schedule_return: provides upgrade link", "https://textmessa.com/pricing" in free_out)

    # 8b: Paid Pro user succeeds
    pro_tools = {t.name: t for t in grocery_tools.build_grocery_tools(PAID_USER)}
    paid_out = await pro_tools["track_package_and_schedule_return"].ainvoke({
        "tracking_number": "1Z9999999999999999",
        "carrier": "auto",
        "notes": "box on front porch",
    })
    check("track_package_and_schedule_return: succeeds for Pro user", "Package Return Concierge Activated" in paid_out)
    check("track_package_and_schedule_return: auto-detects UPS", "UPS #1Z9999999999999999" in paid_out)
    check("track_package_and_schedule_return: provides direct UPS link", "ups.com/track?tracknum=1Z9999999999999999" in paid_out)
    check("track_package_and_schedule_return: includes porch instructions", "box on front porch" in paid_out)

    # 8c: Admin user bypasses gating
    admin_tools = {t.name: t for t in grocery_tools.build_grocery_tools(ADMIN_USER)}
    admin_out = await admin_tools["track_package_and_schedule_return"].ainvoke({
        "tracking_number": "9400111899562537624123",
        "carrier": "auto",
    })
    check("track_package_and_schedule_return: succeeds for Admin user", "Package Return Concierge Activated" in admin_out)
    check("track_package_and_schedule_return: auto-detects USPS", "USPS" in admin_out)


# ---------------------------------------------------------------------------
# Part 9: Subagent Registration & Kill Switch
# ---------------------------------------------------------------------------

async def part9_subagent_registration_and_kill_switch():
    import inspect

    # 9a: With kill switch ON (default True)
    config.GROCERY_AGENT_ENABLED = True
    spec = grocery_tools.build_grocery_subagent(USER, AsyncMock())
    check("build_grocery_subagent: name is grocery_agent", spec["name"] == "grocery_agent")
    check("build_grocery_subagent: description covers food, groceries, Instacart", "Instacart" in spec["description"] and "groceries" in spec["description"])

    # 9b: Orchestrator wires build_grocery_subagent
    src = inspect.getsource(registry.build_orchestrator)
    check("build_orchestrator wires build_grocery_subagent", "build_grocery_subagent" in src)
    check("build_orchestrator gates on config.GROCERY_AGENT_ENABLED", "config.GROCERY_AGENT_ENABLED" in src)

    # 9c: Orchestrator prompt mentions grocery_agent when enabled
    config.GROCERY_AGENT_ENABLED = True
    prompt_on = registry._build_system_prompt(USER, connected_slugs=[], app_preferences={})
    check("system prompt (enabled): mentions grocery_agent", "grocery_agent" in prompt_on)
    check("system prompt (enabled): includes food routing guidance", "delegate to grocery_agent" in prompt_on)

    # 9d: With kill switch OFF (False)
    config.GROCERY_AGENT_ENABLED = False
    prompt_off = registry._build_system_prompt(USER, connected_slugs=[], app_preferences={})
    check("system prompt (disabled): omits grocery_agent", "grocery_agent" not in prompt_off)
    check("system prompt (disabled): omits food routing guidance", "delegate to grocery_agent" not in prompt_off)

    config.GROCERY_AGENT_ENABLED = True


# ---------------------------------------------------------------------------
# Part 10: Amazon Remote Cart Staging, Associate Tag & Stock Pre-Check
# ---------------------------------------------------------------------------

async def part10_amazon_cart_staging():
    tools_map = {t.name: t for t in grocery_tools.build_grocery_tools(USER)}
    check("build_grocery_tools includes stage_amazon_cart", "stage_amazon_cart" in tools_map)

    # 10a: Normal In-Stock Cart Staging
    profile_row = FakeRow(user_id=1, preferred_store="whole_foods")
    pantry_rows = [
        FakeRow(id=1, user_id=1, item_name="olive oil", preferred_brand="Kirkland Signature"),
    ]
    order_row = FakeRow(id=201, user_id=1, cart_provider="amazon", store_name="Amazon", items_count=2, estimated_total=54.98, checkout_url="https://...", status="staged")

    conn = FakeConn(has_tables=True, fetchrow_queue=[profile_row, order_row], fetch_queue=[pantry_rows])
    install_fake_pool(conn)

    amazon_out = await tools_map["stage_amazon_cart"].ainvoke({
        "items_list": ["olive oil", "quaker oats"],
    })

    check("stage_amazon_cart: enriches brand from pantry", "Kirkland" in amazon_out)
    check("stage_amazon_cart: formats remote cart link with associate tag messa2026-20", "AssociateTag=messa2026-20" in amazon_out)
    check("stage_amazon_cart: remote cart URL targets /gp/aws/cart/add.html", "https://www.amazon.com/gp/aws/cart/add.html" in amazon_out)
    check("stage_amazon_cart: includes 1-tap checkout prompt", "[Review Amazon Cart & Checkout ➔]" in amazon_out)
    check("stage_amazon_cart: attaches tag to individual product link", "tag=messa2026-20" in amazon_out)

    # 10b: Stock Pre-Check & Proactive Substitution for Out-of-Stock Item
    # Kirkland Jasmine Rice (B004T3408Y) is out-of-stock on Amazon -> substituted with Iberia Jasmine Rice (B077FYLG56)
    conn2 = FakeConn(has_tables=True, fetchrow_queue=[profile_row, order_row], fetch_queue=[[]])
    install_fake_pool(conn2)

    sub_out = await tools_map["stage_amazon_cart"].ainvoke({
        "items_list": ["Kirkland Jasmine Rice 25lb"],
    })

    check("stage_amazon_cart: detects out of stock item and emits note", "currently out of stock on Amazon" in sub_out)
    check("stage_amazon_cart: substitutes in-stock alternative", "Iberia" in sub_out and "Jasmine" in sub_out)
    check("stage_amazon_cart: cart contains substitute ASIN B077FYLG56", "B077FYLG56" in sub_out)

    # 10c: Seamless Store Routing from stage_instacart_cart(store_name='amazon')
    conn3 = FakeConn(has_tables=True, fetchrow_queue=[profile_row, order_row], fetch_queue=[[]])
    install_fake_pool(conn3)

    routed_out = await tools_map["stage_instacart_cart"].ainvoke({
        "store_name": "Amazon",
        "items_list": ["bounty paper towels"],
    })
    check("stage_instacart_cart(store_name='Amazon'): routes to Amazon cart", "Amazon Cart Staged" in routed_out)
    check("stage_instacart_cart(store_name='Amazon'): includes AssociateTag=messa2026-20", "AssociateTag=messa2026-20" in routed_out)


async def main():
    print("Running test_v3_phase7_grocery_agent suite...\n")
    await part1_db_operations()
    await part2_dietary_and_pantry_tools()
    await part3_meal_plan_generator()
    await part4_fridge_vision_inventory()
    await part5_instacart_staging()
    await part6_restaurant_order_staging()
    await part7_pantry_restock_capsule()
    await part8_package_return_gating()
    await part9_subagent_registration_and_kill_switch()
    await part10_amazon_cart_staging()

    print("\n" + "=" * 50)
    if failures:
        print(f"FAILED ({len(failures)} failures):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
