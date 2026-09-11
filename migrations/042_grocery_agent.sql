-- V3 Phase 7: Messa Autonomous Groceries & Lifestyle Concierge (grocery_agent)
-- Implements groceries-agent.md schema:
-- 1. user_dietary_profiles: stores household allergies, diets, disliked ingredients, preferred grocery stores
-- 2. user_pantry_items: tracks pantry staples, categories, preferred brands, and restock cadence
-- 3. grocery_orders: tracks staged Instacart carts, DoorDash/Uber Eats orders, totals, and express checkout URLs

CREATE TABLE IF NOT EXISTS user_dietary_profiles (
    user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    dietary_flags TEXT[] DEFAULT '{}',          -- e.g. '{"gluten-free", "high-protein", "keto"}'
    allergies TEXT[] DEFAULT '{}',              -- e.g. '{"peanuts", "shellfish", "dairy"}'
    disliked_ingredients TEXT[] DEFAULT '{}',  -- e.g. '{"cilantro", "mushrooms"}'
    household_size INT NOT NULL DEFAULT 1,
    preferred_store VARCHAR(100) DEFAULT 'whole_foods',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_dietary_profiles_user ON user_dietary_profiles(user_id);

CREATE TABLE IF NOT EXISTS user_pantry_items (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_name VARCHAR(150) NOT NULL,
    category VARCHAR(50) DEFAULT 'staple',      -- staple | dairy | produce | protein | snack | beverage
    preferred_brand VARCHAR(100),               -- e.g. 'Oatly Full Fat', 'Vital Farms'
    restock_frequency_days INT,                -- e.g. 7 (weekly) or 14 (bi-weekly)
    last_purchased_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_pantry_user ON user_pantry_items(user_id);

CREATE TABLE IF NOT EXISTS grocery_orders (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cart_provider VARCHAR(50) NOT NULL,         -- 'instacart' | 'doordash' | 'ubereats'
    store_name VARCHAR(150) NOT NULL,
    items_count INT NOT NULL,
    estimated_total NUMERIC(10, 2),
    checkout_url TEXT NOT NULL,
    status VARCHAR(30) DEFAULT 'staged',        -- staged | completed | expired
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_grocery_orders_user ON grocery_orders(user_id);
