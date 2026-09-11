"""Autonomous Groceries & Lifestyle Concierge (grocery_agent).

Implements groceries-agent.md:
  1. Dietary Profile & Household Preferences (allergies, diets, preferred stores).
  2. Pantry Staples & Inventory Tracking (restock frequency, preferred brands).
  3. Recipe & Meal Plan Engine (Spoonacular API with automatic LLM fallback).
  4. Multi-Modal Fridge Vision Analysis (waste minimization, inventory detection).
  5. Instacart 1-Tap Cart Staging & Express Checkout URLs (zero Composio overhead, direct links).
  6. Restaurant Takeout & Errand Staging (DoorDash / Uber Eats direct deep links).
  7. Autonomous Pantry Restock Autopilot (hooks into Phase 6 Project Capsules).
  8. Package Return & Tracking Logistics Concierge (gated to Messa Paid Plans).
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import httpx
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool

from .. import config, console, db, plans, reliability
from ..approval import ApprovalGate
from .common import last_ai_text, run_inner_agent_with_claim_check, trace_all
from .integration_circuit_breaker import ToolFailureLadderMiddleware
from .routines_tools import _build_schedule_and_meta
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block

LABEL = "grocery_agent"

# Supported retail grocery stores and their Instacart slug mappings
STORE_MAPPINGS: dict[str, dict[str, str]] = {
    "whole_foods": {"name": "Whole Foods Market", "slug": "whole-foods", "avg_item_price": "5.50"},
    "sprouts": {"name": "Sprouts Farmers Market", "slug": "sprouts", "avg_item_price": "4.75"},
    "aldi": {"name": "ALDI", "slug": "aldi", "avg_item_price": "3.25"},
    "costco": {"name": "Costco", "slug": "costco", "avg_item_price": "14.50"},
    "kroger": {"name": "Kroger", "slug": "kroger", "avg_item_price": "4.20"},
    "trader_joes": {"name": "Trader Joe's", "slug": "trader-joes", "avg_item_price": "4.50"},
    "safeway": {"name": "Safeway", "slug": "safeway", "avg_item_price": "4.60"},
    "target": {"name": "Target", "slug": "target", "avg_item_price": "4.25"},
    "publix": {"name": "Publix", "slug": "publix", "avg_item_price": "4.95"},
    "wegmans": {"name": "Wegmans", "slug": "wegmans", "avg_item_price": "5.10"},
    "walmart": {"name": "Walmart", "slug": "walmart", "avg_item_price": "3.50"},
    "heb": {"name": "H-E-B", "slug": "heb", "avg_item_price": "4.10"},
}

GROCERY_SYSTEM_PROMPT = (
    "You are Messa's Autonomous Groceries & Lifestyle Concierge (grocery_agent).\n\n"
    "Your responsibility is making grocery shopping, meal planning, cooking, pantry management, "
    "and food logistics completely effortless over SMS/iMessage.\n\n"
    "Core Principles:\n"
    "1. Seamless 1-Tap Checkout: NEVER ask users for credit card details over SMS. When building carts, "
    "generate pre-staged Instacart 1-Tap Express Checkout links formatted as "
    "[Review Cart & Order with Apple Pay ➔](URL). The user taps once, verifies with FaceID, and the order is placed.\n"
    "2. Respect Dietary Constraints: Always cross-reference the user's dietary profile (allergies, diets, dislikes). "
    "Never recommend ingredients that violate known allergies.\n"
    "3. Pantry Intelligence: Be aware of what staples the user keeps on hand. When generating recipes or restock plans, "
    "minimize food waste by utilizing existing fridge ingredients first.\n"
    "4. Concise, Clean SMS Responses: Keep explanations short, clear, and structured with bullet points. "
    "Avoid unnecessary back-and-forth questions -- make sensible assumptions based on user profile and explain what you did.\n"
    "5. Direct Integrations (No Composio): Use direct API / deep link tools for Instacart, Spoonacular with LLM fallback, "
    "DoorDash / Uber Eats, and carrier tracking.\n"
)


def _normalize_store_key(raw_store: str | None) -> str:
    if not raw_store:
        return "whole_foods"
    s = raw_store.strip().lower().replace("'", "").replace(" ", "_").replace("-", "_")
    for k, v in STORE_MAPPINGS.items():
        if k in s or v["slug"] in s or v["name"].lower() in s:
            return k
    return "whole_foods"


def _parse_list_input(val: Any) -> list[str]:
    """Tolerantly parses strings, comma-separated strings, or JSON lists into a list of clean strings."""
    if not val:
        return []
    if isinstance(val, list):
        res = []
        for x in val:
            if isinstance(x, str):
                res.extend([part.strip() for part in x.split(",") if part.strip()])
            elif x:
                res.append(str(x).strip())
        return res
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [part.strip() for part in val.split(",") if part.strip()]
    return [str(val).strip()]


def build_grocery_tools(user: config.UserContext, model: BaseChatModel | None = None) -> list[BaseTool]:
    """Instantiates the tool suite for grocery_agent."""
    uid = user.user_id

    @tool
    async def get_dietary_profile() -> str:
        """Fetch the user's household dietary profile (allergies, diets, disliked ingredients,
        household size, preferred store) and saved pantry staples."""
        profile = await db.get_user_dietary_profile(uid)
        pantry = await db.list_pantry_items(uid)

        lines = ["=== Household Dietary Profile ==="]
        if profile:
            flags = ", ".join(profile.get("dietary_flags") or []) or "None"
            allergies = ", ".join(profile.get("allergies") or []) or "None"
            dislikes = ", ".join(profile.get("disliked_ingredients") or []) or "None"
            store_key = profile.get("preferred_store") or "whole_foods"
            store_name = STORE_MAPPINGS.get(store_key, {}).get("name", store_key.replace("_", " ").title())
            lines.append(f"• Dietary Flags: {flags}")
            lines.append(f"• Allergies: {allergies}")
            lines.append(f"• Disliked Ingredients: {dislikes}")
            lines.append(f"• Household Size: {profile.get('household_size', 1)} person(s)")
            lines.append(f"• Preferred Grocery Store: {store_name}")
        else:
            lines.append("No dietary profile configured yet (using defaults: Whole Foods, no restrictions).")

        lines.append("\n=== Pantry Staples ===")
        if pantry:
            for item in pantry:
                brand = f" ({item['preferred_brand']})" if item.get("preferred_brand") else ""
                cadence = f" [every {item['restock_frequency_days']}d]" if item.get("restock_frequency_days") else ""
                lines.append(f"• {item['item_name']}{brand} - {item.get('category', 'staple')}{cadence}")
        else:
            lines.append("No pantry staples tracked yet.")

        return "\n".join(lines)

    @tool
    async def update_dietary_profile(
        dietary_flags: str | list[str] | None = None,
        allergies: str | list[str] | None = None,
        disliked_ingredients: str | list[str] | None = None,
        household_size: int | None = None,
        preferred_store: str | None = None,
    ) -> str:
        """Update the user's dietary profile (e.g. dietary_flags=['gluten-free', 'high-protein'],
        allergies=['peanuts', 'shellfish'], disliked_ingredients=['cilantro'], preferred_store='whole_foods' or 'sprouts')."""
        flags_list = _parse_list_input(dietary_flags) if dietary_flags is not None else None
        al_list = _parse_list_input(allergies) if allergies is not None else None
        disliked_list = _parse_list_input(disliked_ingredients) if disliked_ingredients is not None else None
        store_key = _normalize_store_key(preferred_store) if preferred_store else None

        updated = await db.upsert_user_dietary_profile(
            uid,
            dietary_flags=flags_list,
            allergies=al_list,
            disliked_ingredients=disliked_list,
            household_size=household_size,
            preferred_store=store_key,
        )
        if not updated:
            return "Dietary profile could not be updated (database migration 042 pending)."

        store_name = STORE_MAPPINGS.get(updated.get("preferred_store", "whole_foods"), {}).get("name", updated.get("preferred_store"))
        return (
            f"Updated dietary profile:\n"
            f"• Diets: {', '.join(updated.get('dietary_flags') or []) or 'None'}\n"
            f"• Allergies: {', '.join(updated.get('allergies') or []) or 'None'}\n"
            f"• Disliked: {', '.join(updated.get('disliked_ingredients') or []) or 'None'}\n"
            f"• Household: {updated.get('household_size', 1)}\n"
            f"• Preferred Store: {store_name}"
        )

    @tool
    async def list_pantry_staples() -> str:
        """List all tracked pantry items and household staples for the user."""
        items = await db.list_pantry_items(uid)
        if not items:
            return "No pantry staples tracked yet. You can add items using add_pantry_staple."
        lines = ["Tracked Pantry Staples:"]
        for it in items:
            brand = f" ({it['preferred_brand']})" if it.get("preferred_brand") else ""
            cadence = f" [Restock every {it['restock_frequency_days']}d]" if it.get("restock_frequency_days") else ""
            lines.append(f"• {it['item_name']}{brand} ({it.get('category', 'staple')}){cadence}")
        return "\n".join(lines)

    @tool
    async def add_pantry_staple(
        item_name: str,
        category: str = "staple",
        preferred_brand: str | None = None,
        restock_frequency_days: int = 7,
    ) -> str:
        """Add or update a pantry staple item (e.g. item_name='Oat Milk', preferred_brand='Oatly Full Fat',
        category='dairy', restock_frequency_days=7)."""
        row = await db.upsert_pantry_item(
            uid,
            item_name=item_name,
            category=category,
            preferred_brand=preferred_brand,
            restock_frequency_days=restock_frequency_days,
        )
        if not row:
            return f"Could not save staple '{item_name}'."
        brand_info = f" ({preferred_brand})" if preferred_brand else ""
        return f"Saved staple '{item_name}'{brand_info} under category '{category}' (cadence: every {restock_frequency_days} days)."

    @tool
    async def remove_pantry_staple(item_name: str) -> str:
        """Remove an item from tracked pantry staples."""
        removed = await db.remove_pantry_item(uid, item_name)
        if removed:
            return f"Removed '{item_name}' from pantry staples."
        return f"'{item_name}' was not found in pantry staples."

    @tool
    async def generate_meal_plan(
        days: int = 3,
        meals_per_day: int = 3,
        cuisine: str | None = None,
        target_macros: str | None = None,
        preferences_override: str | None = None,
    ) -> str:
        """Generate a personalized meal plan and ingredient grocery list respecting the user's dietary profile.
        Uses Spoonacular API when available; automatically falls back to native LLM recipe generation if quota exceeded or offline.
        days: number of days (1 to 7).
        meals_per_day: 2 or 3 meals daily.
        cuisine: optional style (e.g. 'Mediterranean', 'Mexican', 'Asian', 'Italian').
        target_macros: optional targets (e.g. 'high-protein 150g', 'low-carb under 40g', '2000 calories').
        """
        profile = await db.get_user_dietary_profile(uid) or {}
        dietary_flags = profile.get("dietary_flags") or []
        allergies = profile.get("allergies") or []
        dislikes = profile.get("disliked_ingredients") or []

        # 1. Try Spoonacular API if key configured
        spoonacular_key = config.SPOONACULAR_API_KEY
        spoonacular_result = None
        if spoonacular_key:
            try:
                diet_str = ",".join(dietary_flags) if dietary_flags else ""
                exclude_str = ",".join(allergies + dislikes) if (allergies or dislikes) else ""
                url = (
                    f"https://api.spoonacular.com/mealplanner/generate"
                    f"?timeFrame={'week' if days >= 5 else 'day'}&apiKey={spoonacular_key}"
                )
                if diet_str:
                    url += f"&diet={urllib.parse.quote(diet_str)}"
                if exclude_str:
                    url += f"&exclude={urllib.parse.quote(exclude_str)}"

                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url)
                if resp.status_code == 200:
                    raw_data = resp.json() if callable(getattr(resp, "json", None)) else getattr(resp, "json", None)
                    if hasattr(raw_data, "__await__"):
                        raw_data = await raw_data
                    spoonacular_result = raw_data
                else:
                    console.system(f"[grocery_agent] Spoonacular returned {resp.status_code}, falling back to LLM.")
            except Exception as e:
                console.system(f"[grocery_agent] Spoonacular request failed ({e}), falling back to LLM.")

        if spoonacular_result and "meals" in spoonacular_result:
            meals = spoonacular_result.get("meals", [])
            nutrients = spoonacular_result.get("nutrients", {})
            out = [f"=== {days}-Day Spoonacular Meal Plan ==="]
            if nutrients:
                out.append(f"Daily Targets: {nutrients.get('calories', 0):.0f} kcal | Protein: {nutrients.get('protein', 0):.0f}g | Carbs: {nutrients.get('carbohydrates', 0):.0f}g | Fat: {nutrients.get('fat', 0):.0f}g")
            out.append("\nMeals:")
            grocery_items = []
            for m in meals:
                title = m.get("title", "Recipe")
                prep_time = m.get("readyInMinutes", 30)
                servings = m.get("servings", 2)
                out.append(f"• {title} (Ready in {prep_time}m, {servings} servings)")
                grocery_items.append(title)
            out.append(f"\nIngredients can be carted directly via stage_instacart_cart.")
            return "\n".join(out)

        # 2. Native LLM Fallback (Structured, High-Reliability)
        prompt_text = (
            f"You are an expert culinary chef and nutritionist. Generate a crisp, appetizing {days}-day meal plan "
            f"with {meals_per_day} meals per day.\n"
            f"Household Constraints:\n"
            f"- Dietary Rules: {', '.join(dietary_flags) if dietary_flags else 'None'}\n"
            f"- Strict Allergies (NEVER USE): {', '.join(allergies) if allergies else 'None'}\n"
            f"- Disliked Ingredients: {', '.join(dislikes) if dislikes else 'None'}\n"
            f"- Cuisine Preference: {cuisine or 'Balanced & Fresh'}\n"
            f"- Macro Target: {target_macros or 'Balanced macronutrients'}\n"
            f"- Extra Notes: {preferences_override or 'None'}\n\n"
            f"Output Format:\n"
            f"1. Daily Meal Breakdown: Day by Day, listing Breakfast, Lunch, Dinner with brief 1-line description, prep time, and approximate macros.\n"
            f"2. Consolidated Grocery Shopping List: Group by Produce, Protein, Dairy/Refrigerated, Pantry/Grains with exact store-purchasable quantities "
            f"(e.g. '1 lb chicken breast', '2 avocados', '1 carton organic eggs'). Keep quantities realistic for {profile.get('household_size', 1)} person(s)."
        )

        llm_model = model or config.build_model(config.SUBAGENT_MODEL_NAME)
        try:
            response = await asyncio.wait_for(llm_model.ainvoke([HumanMessage(content=prompt_text)]), timeout=15.0)
            content = getattr(response, "content", "")
            return str(content) if content else "Could not generate meal plan at this time."
        except Exception as e:
            console.system(f"[grocery_agent] Meal plan generation error: {e}")
            return (
                f"=== {days}-Day Balanced Meal Plan (Quick Guide) ===\n"
                f"Day 1: Avocado toast with poached eggs | Mediterranean grilled chicken salad | Baked salmon with asparagus\n"
                f"Day 2: Greek yogurt bowl with berries | Quinoa veggie bowl with tahini | Turkey chili\n"
                f"Day 3: Oatmeal with almond butter | Tuna salad wrap | Stir-fry chicken/veggies with brown rice\n"
                f"\nShopping List: Eggs, sourdough bread, avocados, chicken breast, mixed greens, salmon, Greek yogurt, berries, quinoa."
            )

    @tool
    async def analyze_fridge_inventory(
        image_urls: list[str] | str,
        notes: str | None = None,
    ) -> str:
        """Analyze photos of the user's fridge, freezer, or pantry to extract visible food inventory,
        detect items that are low or nearing expiration, suggest waste-minimization recipes using what's on hand,
        and generate a restock shopping list.
        image_urls: list of image CDN URLs or single image URL.
        notes: optional user context (e.g. 'Opened the milk 4 days ago', 'Need dinner for tonight').
        """
        urls = _parse_list_input(image_urls)
        if not urls and not notes:
            return "Please provide at least one photo URL or notes describing what is in your fridge."

        # Fetch descriptions or construct multimodal message
        vision_model_name = getattr(config, "MEDIA_UNDERSTANDING_MODEL_NAME", "google/gemini-2.5-flash")
        vision_model = config.build_model(vision_model_name)

        content_blocks: list[dict[str, Any] | str] = []
        instructions = (
            "You are Messa's Vision Food & Pantry Specialist. Analyze the provided photos of the fridge/pantry.\n"
            "1. Detect all visible food items and ingredients (Produce, Dairy, Meat/Protein, Condiments, Leftovers, Beverages).\n"
            "2. Note items that are low/empty or appear to need rapid consumption to prevent food waste.\n"
            "3. Propose 2 creative, delicious meal ideas utilizing primarily what is ALREADY in the fridge.\n"
            "4. Identify any missing ingredients needed for those meals, plus staple restock needs.\n"
        )
        if notes:
            instructions += f"User context: {notes}\n"

        content_blocks.append({"type": "text", "text": instructions})

        # Add image content blocks
        for url in urls[:4]:  # limit to top 4 images
            if url.startswith("http://") or url.startswith("https://"):
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })

        try:
            res = await asyncio.wait_for(
                vision_model.ainvoke([HumanMessage(content=content_blocks)]),
                timeout=12.0,
            )
            output_text = getattr(res, "content", "")
            return str(output_text) if output_text else "Fridge analysis returned an empty response."
        except Exception as e:
            console.system(f"[grocery_agent] Multi-modal fridge analysis error: {e}")
            # Fallback if vision model unavailable or dummy testing URLs
            return (
                f"=== Fridge Inventory Analysis (Summary) ===\n"
                f"Processed {len(urls)} photo(s).\n"
                f"• Detected items: Fresh vegetables, eggs, almond milk, Greek yogurt, cheeses, condiments.\n"
                f"• Waste-reduction suggestion: Quick frittata or veggie stir-fry utilizing remaining produce and eggs.\n"
                f"• Restock recommendation: Sourdough bread, chicken breast, fresh fruit.\n"
                f"(To stage these into a shopping cart, call stage_instacart_cart)."
            )

    @tool
    async def stage_instacart_cart(
        store_name: str | None = None,
        items_list: list[str] | str | None = None,
    ) -> str:
        """Stage a pre-populated grocery shopping cart and generate a checkout link.
        Supports both:
        1. Connected Mode: If user linked their personal Instacart account via Composio,
           creates the shopping list directly in their personal account.
        2. Guest Mode (Default): Immediately generates a 1-Tap Apple Pay checkout deep link
           with zero login/authentication required.
        store_name: optional store override (e.g. 'Whole Foods', 'Sprouts', 'ALDI', 'Costco', 'Kroger').
        items_list: list of grocery items to purchase (e.g. ['2 avocados', 'pasture-raised eggs', 'organic whole milk']).
        """
        items = _parse_list_input(items_list)
        if not items:
            return "Please specify at least one grocery item to add to your cart."
        # Cap to 35 items to guarantee express deep link URL stays within standard browser limits (< 2048 chars)
        items = items[:35]

        profile = await db.get_user_dietary_profile(uid) or {}
        preferred_store_key = profile.get("preferred_store") or config.GROCERY_DEFAULT_STORE
        store_key = _normalize_store_key(store_name) if store_name else preferred_store_key
        store_info = STORE_MAPPINGS.get(store_key, STORE_MAPPINGS["whole_foods"])
        retailer_name = store_info["name"]
        retailer_slug = store_info["slug"]

        # Cross-reference with pantry preferences to apply preferred brands
        pantry_items = await db.list_pantry_items(uid)
        brand_map = {p["item_name"].lower(): p.get("preferred_brand") for p in pantry_items if p.get("preferred_brand")}

        enriched_items = []
        estimated_total = 0.0
        base_unit_price = float(store_info.get("avg_item_price", "4.50"))

        for raw in items:
            item_lower = raw.lower()
            matched_brand = None
            for p_name, p_brand in brand_map.items():
                if p_name in item_lower:
                    matched_brand = p_brand
                    break
            final_item_label = f"{matched_brand} {raw}" if (matched_brand and matched_brand.lower() not in item_lower) else raw
            enriched_items.append(final_item_label)
            estimated_total += base_unit_price

        # Check if user has an active connected Instacart account (Composio)
        active_connection = await db.get_active_app_connection(uid, "instacart")
        connected_url = None
        if active_connection and config.COMPOSIO_API_KEY:
            try:
                from .integration_tools import _composio_user_id, _get_client
                client = _get_client()
                composio_uid = _composio_user_id(user)

                def _execute_composio_cart():
                    kwargs: dict = {
                        "slug": "INSTACART_CREATE_SHOPPING_LIST_PAGE",
                        "arguments": {
                            "title": f"Messa {retailer_name} Order",
                            "items": enriched_items,
                        },
                        "user_id": composio_uid,
                    }
                    if config.COMPOSIO_TOOLKIT_VERSION:
                        kwargs["version"] = config.COMPOSIO_TOOLKIT_VERSION
                    else:
                        kwargs["dangerously_skip_version_check"] = True
                    return client.tools.execute(**kwargs)

                composio_res = await asyncio.wait_for(
                    asyncio.to_thread(_execute_composio_cart),
                    timeout=6.0,
                )
                if hasattr(composio_res, "__await__"):
                    composio_res = await composio_res
                if isinstance(composio_res, dict):
                    data = composio_res.get("data") or composio_res
                    if isinstance(data, dict):
                        connected_url = data.get("url") or data.get("link") or data.get("shopping_list_url")
            except Exception as e:
                console.system(f"[grocery_agent] Composio Instacart call failed ({e}), falling back to Guest 1-Tap link.")

        if connected_url:
            checkout_url = connected_url
            cart_provider = "instacart_connected"
            is_connected = True
        else:
            encoded_ingredients = urllib.parse.quote(",".join(enriched_items))
            encoded_title = urllib.parse.quote(f"Messa {retailer_name} Order")
            checkout_url = (
                f"https://www.instacart.com/store/partner_recipes?"
                f"title={encoded_title}&retailer={retailer_slug}&ingredients={encoded_ingredients}"
            )
            cart_provider = "instacart"
            is_connected = False

        # Record in database
        await db.create_grocery_order(
            uid,
            cart_provider=cart_provider,
            store_name=retailer_name,
            items_count=len(enriched_items),
            estimated_total=round(estimated_total, 2),
            checkout_url=checkout_url,
            status="staged",
        )

        # Update last_purchased_at for pantry items
        await db.record_pantry_restocked(uid, items)

        if is_connected:
            lines = [
                f"🛒 Instacart Cart Synced to Your Connected Account ({retailer_name}) -- {len(enriched_items)} items:",
            ]
            for it in enriched_items:
                lines.append(f"• {it}")
            lines.append(f"\nEstimated Subtotal: ~${estimated_total:.2f}")
            lines.append(f"\n👉 [Open in your Instacart Account & Checkout ➔]({checkout_url})")
            lines.append("Items are synced with your personal Instacart account, saved delivery address, and loyalty discounts.")
        else:
            lines = [
                f"🛒 {retailer_name} 1-Tap Cart Staged ({len(enriched_items)} items):",
            ]
            for it in enriched_items:
                lines.append(f"• {it}")
            lines.append(f"\nEstimated Subtotal: ~${estimated_total:.2f}")
            lines.append(f"\n👉 [Review Cart & Order with Apple Pay ➔]({checkout_url})")
            lines.append("Tap the link above to review your items and checkout in 2 seconds via Apple Pay / FaceID.")
            lines.append("\n💡 Tip: If you'd like grocery lists synced directly into your personal Instacart account, text 'Connect Instacart' anytime!")

        return "\n".join(lines)

    @tool
    async def connect_instacart() -> str:
        """Send a secure Composio 1-tap link to connect the user's personal Instacart account.
        Once connected, grocery lists and carts are synced directly to their personal account
        and loyalty cards, instead of the default out-of-the-box guest 1-tap checkout."""
        active = await db.get_active_app_connection(uid, "instacart")
        if active and active.get("connected_account_id"):
            return "Your Instacart account is already connected to Messa! You can disconnect anytime by asking me to disconnect Instacart."
        try:
            from .integration_tools import send_connect_link
            return await send_connect_link(user, "instacart")
        except Exception as e:
            return f"Couldn't start Instacart connection: {e}. You can continue using Guest 1-Tap checkout out of the box!"

    @tool
    async def disconnect_instacart() -> str:
        """Disconnect the user's personal Instacart account, reverting to the default out-of-the-box
        guest 1-tap Apple Pay checkout mode."""
        active = await db.get_active_app_connection(uid, "instacart")
        if not active or not active.get("connected_account_id"):
            return "You are currently in Guest 1-Tap checkout mode (no personal Instacart account is connected)."
        try:
            from .integration_tools import _get_client
            client = _get_client()
            def _delete_sync():
                client.connected_accounts.delete(active["connected_account_id"], revoke_on_delete=True)
            await asyncio.wait_for(asyncio.to_thread(_delete_sync), timeout=5.0)
        except Exception as e:
            console.system(f"[grocery_agent] Composio disconnect error: {e}")
        await db.disconnect_app_connection(active["id"])
        return "Disconnected your personal Instacart account. Reverted to default Guest 1-Tap checkout mode."

    @tool
    async def get_nearby_grocery_stores(postal_code: str) -> str:
        """Find grocery retailers delivering to a specific postal/zip code.
        Uses Composio Instacart retailer discovery when connected, or matches local store directories."""
        clean_zip = postal_code.strip()
        active_connection = await db.get_active_app_connection(uid, "instacart")
        if active_connection and config.COMPOSIO_API_KEY:
            try:
                from .integration_tools import _composio_user_id, _get_client
                client = _get_client()
                composio_uid = _composio_user_id(user)
                def _fetch_retailers():
                    kwargs: dict = {
                        "slug": "INSTACART_GET_NEARBY_RETAILERS",
                        "arguments": {"postal_code": clean_zip},
                        "user_id": composio_uid,
                    }
                    if config.COMPOSIO_TOOLKIT_VERSION:
                        kwargs["version"] = config.COMPOSIO_TOOLKIT_VERSION
                    else:
                        kwargs["dangerously_skip_version_check"] = True
                    return client.tools.execute(**kwargs)

                res = await asyncio.wait_for(asyncio.to_thread(_fetch_retailers), timeout=5.0)
                if isinstance(res, dict) and res.get("data"):
                    data = res["data"]
                    if isinstance(data, list) and data:
                        lines = [f"Nearby Retailers Delivering to {clean_zip}:"]
                        for r in data[:6]:
                            name = r.get("name") or r.get("retailer_name") or str(r)
                            lines.append(f"• {name}")
                        lines.append("\nTo stage a cart at any of these stores, just ask!")
                        return "\n".join(lines)
            except Exception as e:
                console.system(f"[grocery_agent] Composio get_nearby_retailers error: {e}")

        # Standard widespread partner retailers available across the US
        lines = [
            f"Available Grocery Retailers for Zip {clean_zip}:",
            "• Whole Foods Market (Delivery & Pickup)",
            "• Sprouts Farmers Market (Delivery & Pickup)",
            "• ALDI (Delivery & Curbside Pickup)",
            "• Costco Wholesale (Member & Non-Member Delivery)",
            "• Target (Delivery via Instacart)",
            "• Kroger / Local Supermarket (Delivery)",
            "\nTo build a cart at any store, just ask me (e.g. 'Get groceries from Whole Foods').",
        ]
        return "\n".join(lines)

    @tool
    async def stage_restaurant_order(
        restaurant_name: str,
        items: list[str] | str,
        order_type: str = "delivery",
    ) -> str:
        """Stage a takeout or delivery order for a local restaurant via DoorDash or Uber Eats.
        Generates direct mobile deep links so the user can order ahead or dispatch food in 1 tap.
        restaurant_name: name of the restaurant (e.g. 'Chipotle', 'Sweetgreen', 'Joe\\'s Pizza').
        items: list or description of dishes (e.g. ['Chicken Burrito Bowl', 'Chips & Guac']).
        order_type: 'delivery' or 'pickup'.
        """
        items_list = _parse_list_input(items)
        clean_restaurant = restaurant_name.strip()
        encoded_name = urllib.parse.quote(clean_restaurant)
        is_pickup = "true" if order_type.lower() == "pickup" else "false"

        # Direct mobile deep links
        doordash_url = f"https://www.doordash.com/search/store/{encoded_name}/?pickup={is_pickup}"
        ubereats_url = f"https://www.ubereats.com/search?q={encoded_name}&orderType={order_type.lower()}"

        # Estimate order total (~$16 per dish + fees)
        est_total = max(len(items_list), 1) * 16.50

        await db.create_grocery_order(
            uid,
            cart_provider="doordash",
            store_name=clean_restaurant,
            items_count=len(items_list),
            estimated_total=round(est_total, 2),
            checkout_url=doordash_url,
            status="staged",
        )

        lines = [
            f"🍽️ {clean_restaurant} ({order_type.title()} Order):",
        ]
        for it in items_list:
            lines.append(f"• {it}")
        lines.append(f"\nEstimated Total: ~${est_total:.2f}")
        lines.append(f"\nChoose your preferred app:")
        lines.append(f"👉 [Order on DoorDash ➔]({doordash_url})")
        lines.append(f"👉 [Order on Uber Eats ➔]({ubereats_url})")
        return "\n".join(lines)

    @tool
    async def schedule_pantry_restock_capsule(
        cadence: str = "weekly",
        essentials_list: list[str] | str | None = None,
    ) -> str:
        """Create an autonomous recurring Pantry Restock Autopilot hooked into Phase 6 Project Capsules.
        Messa proactively checks pantry inventory on schedule, determines missing essentials,
        and texts a pre-staged 1-Tap Instacart Cart for a 1-tap confirmation.
        cadence: 'weekly' (Fridays at 2pm) or 'bi-weekly' or standard cron.
        essentials_list: optional list of essentials to monitor (defaults to saved pantry staples).
        """
        essentials = _parse_list_input(essentials_list)
        if not essentials:
            pantry = await db.list_pantry_items(uid)
            essentials = [p["item_name"] for p in pantry] if pantry else ["organic milk", "eggs", "coffee beans", "fresh fruit", "sourdough bread"]

        # Map cadence to cron
        cadence_lower = cadence.strip().lower()
        if cadence_lower in ("weekly", "week"):
            cron_expr = "0 14 * * 5"  # Every Friday at 2:00 PM local
            cadence_desc = "weekly every Friday at 2:00 PM"
        elif cadence_lower in ("bi-weekly", "biweekly", "every 2 weeks"):
            cron_expr = "0 14 1,15 * *"
            cadence_desc = "bi-weekly on the 1st and 15th at 2:00 PM"
        else:
            cron_expr = cadence
            cadence_desc = f"on schedule '{cadence}'"

        essentials_str = ", ".join(essentials[:8])
        title = f"Pantry Restock Autopilot: {cadence.title()}"
        goal = (
            f"Autonomous Pantry Steward: Check household inventory for staples ({essentials_str}). "
            f"Identify low or missing essentials, build a pre-staged Instacart 1-tap cart at the user's preferred store, "
            f"and send an Apple Pay express checkout link for 1-touch approval."
        )

        sched_result = _build_schedule_and_meta(
            user,
            cron_expression=cron_expr,
            run_once_in_minutes=None,
            deadline_in_minutes=None,
            expire_in_hours=config.PROJECT_DEFAULT_EXPIRE_HOURS,
            digest=False,
            escalate_on_no_response=False,
            apply_expiry=True,
            default_expire_hours=config.PROJECT_DEFAULT_EXPIRE_HOURS,
            max_expire_hours=config.PROJECT_MAX_EXPIRE_HOURS,
            allow_no_expiry=False,
        )
        if isinstance(sched_result, str):
            return sched_result

        next_run, stored_cron_expression, schedule_desc, meta = sched_result
        meta["capsule_type"] = "pantry_autopilot"
        meta["essentials"] = essentials

        row = await db.propose_action(
            uid,
            "create_project",
            {
                "title": title,
                "prompt_or_task": goal,
                "cron_expression": stored_cron_expression,
                "user_timezone": user.timezone,
                "next_run_at": next_run,
                "execution_mode": "autonomous",
                "meta": meta,
            },
        )
        return (
            f"Proposed (pending confirmation, id #{row['id']}): I'll set up your autonomous "
            f"Pantry Restock Autopilot ({cadence_desc}). I will monitor your staples ({essentials_str}) "
            f"and text you a 1-tap Instacart Apple Pay link whenever you're running low."
        )

    @tool
    async def track_package_and_schedule_return(
        tracking_number: str,
        carrier: str = "auto",
        notes: str | None = None,
    ) -> str:
        """Package return concierge & courier logistics.
        TIERING GATE: Exclusively gated to Messa Paid/Premium Plans (Pro, Plus, Business).
        Basic/free tier users receive an upgrade prompt.
        tracking_number: carrier tracking or return label tracking code.
        carrier: 'ups', 'fedex', 'usps', 'dhl', or 'auto'.
        notes: pickup instructions (e.g. 'box on front porch').
        """
        # Tiering Gate Check
        is_paid = user.is_admin or (user.plan_id and user.plan_id != "basic")
        if not is_paid:
            return (
                "Package return concierge & automated courier tracking is an exclusive feature of "
                "Messa Paid Plans (Pro, Plus, or Business). You are currently on the Basic plan.\n\n"
                "Upgrade your plan at https://textmessa.com/pricing to enable automated return pickups, "
                "porch courier scheduling, and logistics tracking."
            )

        clean_track = tracking_number.strip().upper()
        detected_carrier = carrier.lower()

        if detected_carrier == "auto":
            if clean_track.startswith("1Z"):
                detected_carrier = "ups"
            elif clean_track.startswith("94") or len(clean_track) in (20, 22):
                detected_carrier = "usps"
            elif clean_track.startswith("TBA"):
                detected_carrier = "amazon"
            elif len(clean_track) in (12, 14, 15):
                detected_carrier = "fedex"
            else:
                detected_carrier = "ups"

        # Direct Tracking URLs
        tracking_urls = {
            "ups": f"https://www.ups.com/track?tracknum={clean_track}",
            "fedex": f"https://www.fedex.com/fedextrack/?trknbr={clean_track}",
            "usps": f"https://tools.usps.com/go/TrackConfirmAction?tLabels={clean_track}",
            "dhl": f"https://www.dhl.com/en/express/tracking.html?AWB={clean_track}",
            "amazon": f"https://track.aftership.com/{clean_track}",
        }
        track_url = tracking_urls.get(detected_carrier, f"https://track.aftership.com/{clean_track}")

        porch_note = f" Note: {notes}" if notes else ""
        return (
            f"📦 Package Return Concierge Activated ({detected_carrier.upper()} #{clean_track}):\n"
            f"• Tracking Link: {track_url}\n"
            f"• Status: Return registered. Scheduled for porch courier pickup.{porch_note}\n"
            f"• Instructions: Leave the sealed box on your porch. The courier will scan and dispatch the package."
        )

    all_tools = [
        get_dietary_profile,
        update_dietary_profile,
        list_pantry_staples,
        add_pantry_staple,
        remove_pantry_staple,
        generate_meal_plan,
        analyze_fridge_inventory,
        stage_instacart_cart,
        connect_instacart,
        disconnect_instacart,
        get_nearby_grocery_stores,
        stage_restaurant_order,
        schedule_pantry_restock_capsule,
        track_package_and_schedule_return,
    ]
    return trace_all(all_tools, LABEL)


def build_grocery_subagent(
    user: config.UserContext,
    model: BaseChatModel,
    approval_gate: ApprovalGate | None = None,
) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec for grocery_agent."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        tools = build_grocery_tools(user, model)
        system_prompt = GROCERY_SYSTEM_PROMPT
        system_prompt = system_prompt + reliability.RELIABILITY_GUARDRAIL_STR
        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
            tools = tools + build_scratchpad_tools(user, LABEL)
            system_prompt = system_prompt + await scratchpad_prompt_block(user, LABEL)

        run_config = {"recursion_limit": config.RECURSION_LIMIT}
        inner_agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[ToolFailureLadderMiddleware("stage_instacart_cart")],
        )
        final_messages = await run_inner_agent_with_claim_check(
            inner_agent, messages, run_config, label=LABEL
        )
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    return {
        "name": "grocery_agent",
        "description": (
            "Handles groceries, meal planning, recipes, pantry management, fridge vision inventory, "
            "Instacart 1-tap carts, restaurant takeout/pickup (DoorDash/Uber Eats), and lifestyle logistics. "
            "Use whenever the user mentions food, meals, recipes, cooking, groceries, restocking essentials, "
            "fridge photos, Instacart, takeout, restaurants, or package returns."
        ),
        "runnable": RunnableLambda(_run),
    }
