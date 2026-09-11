# Messa Autonomous Groceries & Lifestyle Concierge (`grocery_agent`)
## Technical Architecture & Product Implementation Plan

**Target Feature**: Autonomous Grocery Shopping, Meal Planning, Restaurant Takeout, and Lifestyle Logistics  
**Subagent Name**: `grocery_agent`  
**Integration Layer**: Instacart Developer Platform + DoorDash/Uber Eats + Spoonacular (with LLM Fallback) + Composio  
**Cost Model**: $0 base developer overhead with 3–5% affiliate revenue share on grocery baskets.

---

## 1. Executive Summary & The "Magic" User Experience

Messa Autonomous Groceries transforms Messa from a purely digital executive assistant into a **physical-world household steward**. Rather than forcing the user into tedious web shopping interfaces, Messa handles the entire food lifecycle through natural SMS/iMessage interactions:

1. **"The Snap & Restock" (Vision Inventory)**:
   - User texts photos of their open fridge and pantry.
   - Messa calculates the inventory delta, identifies items nearing expiration, drafts meals to avoid waste, and stages missing ingredients into a cart.
2. **"Calendar-Aware Meal Planning"**:
   - Messa cross-references the user's schedule (e.g. late dinners on Tuesday/Thursday) and only plans groceries for nights the user will actually cook at home.
3. **"Headless 1-Tap Express Checkout"**:
   - Messa builds the cart at the user's preferred local store (Whole Foods, Sprouts, Aldi, Costco, Kroger, etc.) via Instacart Connect.
   - Sends a dynamic deep link over SMS: *[Review Cart & Order with Apple Pay ➔]*. The user taps once, confirms via FaceID in 2 seconds, and the groceries are dispatched.
4. **"Autonomous Pantry Steward" (Phase 6 Project Capsule)**:
   - Operates on a weekly cadence routine. Proactively suggests restocking essentials (milk, coffee beans, fruit) every Friday afternoon with a 1-tap iMessage thumbs-up confirmation.

---

## 2. API Integrations & Cost Optimization Strategy

### A. Core Commerce: Instacart Developer Platform
* **Coverage**: 85,000+ stores across 95% of the US (groceries, pharmacy, pet supplies, household essentials).
* **Cost to Messa**: **$0 (Free API)**.
* **Monetization**: Instacart pays an affiliate revenue share (**3%–5% of cart value**) back to Messa on completed orders.
* **Payment Architecture**:
  - **Merchant of Record (MoR)**: Instacart processes the credit card/Apple Pay, manages sales tax, driver tipping, and handles out-of-stock refunds.
  - **Messa Liability**: Zero PCI-DSS or payment handling liability.

### B. Recipe & Nutrition Engine: Spoonacular + Native LLM Fallback
* **Primary (Spoonacular API)**: Used for precise macro tracking and structured ingredient parsing under the free tier (150 requests/day).
* **Automated LLM Fallback**:
  - Once the free tier daily quota is reached, Messa seamlessly switches to an internal LLM recipe generator (`tools/recipe_generation_prompt`).
  - The LLM parses user cravings, constraints (e.g., *keto, gluten-free, 30-min prep*), and outputs standard ingredient quantities directly formatted for Instacart cart addition.
  - **Result**: Zero API downtime, $0 overage cost.

### C. Restaurant Takeout & Errand Courier: DoorDash / Uber Eats
* **Restaurant Delivery & Pickup**: Allows users to order takeout from local restaurants or order ahead for pickup on the drive home.
* **DoorDash Drive / Uber Direct (White-Label Courier)**: Optional custom courier dispatch for ad-hoc physical errands (passing flat delivery fee through to user).

### D. Package Tracking & Porch Returns: AfterShip / UPS
* **Tiering Gate**: Exclusively gated to **Messa Paid/Premium Plans**.
* Automatically detects package tracking numbers from inbound emails, alerts on delivery, and schedules courier porch pickups for returns.

---

## 3. System Architecture & Component Design

```
                     ┌──────────────────────────────────────────────┐
                     │          User (SMS / iMessage / MMS)         │
                     │    - Fridge photos, cravings, routine texts   │
                     └──────────────────────┬───────────────────────┘
                                            │
                                            ▼
                     ┌──────────────────────────────────────────────┐
                     │            Messa Inbound Pipeline            │
                     │  - Vision OCR/Scene extraction (fridge/food) │
                     │  - Pure acknowledgment short-circuit         │
                     └──────────────────────┬───────────────────────┘
                                            │
                                            ▼
                     ┌──────────────────────────────────────────────┐
                     │             NEW: grocery_agent               │
                     │  - Household dietary memory & allergies      │
                     │  - Recipe math & waste minimization          │
                     │  - Spoonacular API / LLM Fallback engine     │
                     └──────────────┬───────────────────────────────┘
                                    │
          ┌─────────────────────────┴─────────────────────────┐
          ▼                                                   ▼
┌─────────────────────────────────┐       ┌─────────────────────────────────┐
│     Instacart Connect API       │       │    DoorDash / Uber Eats API     │
│ - Local store catalog search    │       │ - Restaurant menu search        │
│ - Cart building & item batching │       │ - Order-ahead pickup staging    │
│ - 1-Tap Express Checkout URLs   │       │ - Deep link generation          │
└─────────────────┬───────────────┘       └─────────────────┬───────────────┘
                  │                                         │
                  └─────────────────────────┬───────────────┘
                                            ▼
                     ┌──────────────────────────────────────────────┐
                     │         Pre-Staged 1-Tap Checkout            │
                     │  - Native iOS app deep link (Apple Pay)      │
                     │  - No credit card entry over SMS             │
                     └──────────────────────────────────────────────┘
```

---

## 4. Database Schema (Neon PostgreSQL Migration 042)

To remember household preferences, dietary constraints, and pantry staples without prompting the user repeatedly:

```sql
-- migrations/042_grocery_agent.sql

CREATE TABLE IF NOT EXISTS user_dietary_profiles (
    user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    dietary_flags TEXT[] DEFAULT '{}',          -- e.g. '{"gluten-free", "high-protein"}'
    allergies TEXT[] DEFAULT '{}',              -- e.g. '{"peanuts", "shellfish"}'
    disliked_ingredients TEXT[] DEFAULT '{}',  -- e.g. '{"cilantro", "mushrooms"}'
    household_size INT NOT NULL DEFAULT 1,
    preferred_store VARCHAR(100) DEFAULT 'whole_foods',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_pantry_items (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_name VARCHAR(150) NOT NULL,
    category VARCHAR(50) DEFAULT 'staple',      -- staple | dairy | produce | protein
    preferred_brand VARCHAR(100),               -- e.g. 'Oatly Full Fat'
    restock_frequency_days INT,                -- e.g. 7 (weekly) or 14 (bi-weekly)
    last_purchased_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_pantry_user ON user_pantry_items(user_id);

CREATE TABLE IF NOT EXISTS grocery_orders (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cart_provider VARCHAR(50) NOT NULL,         -- 'instacart' | 'doordash' | 'uber'
    store_name VARCHAR(150) NOT NULL,
    items_count INT NOT NULL,
    estimated_total NUMERIC(10, 2),
    checkout_url TEXT NOT NULL,
    status VARCHAR(30) DEFAULT 'staged',        -- staged | completed | expired
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_grocery_orders_user ON grocery_orders(user_id);
```

---

## 5. Tools Exposed on `grocery_agent`

The new `grocery_agent` subagent will register the following core tools:

1. `get_dietary_profile()`: Reads dietary flags, allergies, and preferred stores.
2. `update_dietary_profile(dietary_flags, allergies, preferred_store)`: Updates user taste preferences.
3. `generate_meal_plan(days, meals_per_day, cuisine, target_macros)`:
   - Calls Spoonacular API.
   - Falls back to internal LLM generator if Spoonacular quota is exceeded.
4. `analyze_fridge_inventory(image_urls)`: Extracts ingredients detected from user photos and computes restock needs.
5. `stage_instacart_cart(store_name, items_list)`: Searches store inventory, matches preferred brands, batches items, and returns the **1-Tap Express Checkout Link**.
6. `stage_restaurant_order(restaurant_name, items, order_type)`: Stages DoorDash/Uber Eats delivery or pickup order.
7. `schedule_pantry_restock_capsule(cadence, essentials_list)`: Hooks into Phase 6 Project Capsules to create an autonomous recurring pantry autopilot.

---

## 6. Implementation Roadmap

### Phase 7.1: Foundation & Dietary Profile
* Create migration `042_grocery_agent.sql`.
* Implement `messa/tools/grocery_tools.py` with dietary profile management.
* Build Spoonacular client with automatic LLM fallback generator.

### Phase 7.2: Instacart Cart Staging & Express Link
* Connect Instacart Developer Platform API (or Composio Instacart Toolkit).
* Implement `stage_instacart_cart` generating pre-populated mobile deep links.
* Test 1-tap Apple Pay handoff over SMS.

### Phase 7.3: Fridge Vision & Recipe-to-Cart
* Connect multi-image vision analysis to identify pantry inventory and waste-minimization recipes.
* Seamlessly translate recipe ingredients into exact store-purchasable quantities.

### Phase 7.4: Autonomous Autopilot & Paid Logistics Gating
* Hook into Phase 6 Project Capsules for autonomous weekly restock check-ins.
* Implement package return & tracking gated exclusively to paid subscription tiers.
* Full release regression tests across all suites.
