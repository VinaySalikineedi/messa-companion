# Messa V3 Autonomous: The Executive Agent Architecture & Intelligence Blueprint
**Status:** Master Specification & Decision Record  
**Authors:** Vinay Salikineedi & Antigravity  
**Date:** September 10, 2026  
**Target Platform:** Messa Orchestrator, Inbound Webhooks, Browser Proxy, Dynamic Policy Engine, Real-World Execution, Autonomous Project Capsules

---

## 1. Executive Summary & Vision

Messa is not a chatbot; Messa is an **Autonomous Executive Partner**. 

In our live production deep-dive (analyzing the Sept 9–10 investor pitch session), Messa demonstrated raw capability (drafting, editing, staging, and scheduling custom-domain emails over SMS). However, it exposed key limits of turn-based chatbots:
1. **The "Mother-May-I" Approval Trap**: Redundant confirmations after explicit approvals.
2. **Notification Fatigue**: Forwarding 5:51 AM marketing spam via SMS.
3. **Over-Engineered Muting**: Spinning up 30-minute LLM cron jobs to mute newsletter senders.
4. **Turn Latency**: 3 to 7-minute sequential execution chains.
5. **Lack of Long-Lived Goal State**: Tasks are forgotten once the immediate chat turn completes.

To become an **absolute monster** in this game, Messa must operate on two foundational layers:
* **The Reactive Layer (Fast Executive Partner)**: Resolving turns in <10 seconds with high situational awareness and zero redundant friction.
* **The Autonomous Project Layer (Long-Running Mission Capsules)**: Managing complex, multi-week, multi-modal objectives—such as fighting customer service for a refund, executing holiday shopping campaigns, or running an investor pipeline—with self-governing cadences, dedicated vaults, and proactive asynchronous check-ins.

---

## 2. The Core Reliability & Performance Pillars

### Pillar 1: One-Touch Autonomous Approvals & Conflict Auto-Supersede
* **Problem**: When the user explicitly texted *"I like this version! Approved"*, Messa staged it and asked *"Say 'yes' and I'll arm it"*. Changing channels from Gmail to Messa left both scheduled, requiring manual turns to resolve.
* **Architecture**:
  ```
  Incoming User Prompt
          │
          ▼
  [Intent Analysis: Explicit Approval Affirmation?]
          ├── YES ──► Arm & execute action in the EXACT SAME TURN.
          │           (Keywords: "approved", "send it", "looks good", "lock it in")
          └── NO  ──► Stage as draft preview and wait for feedback.
  ```
* **Conflict Auto-Supersede**:
  * If a new schedule/communication is created for a recipient with an existing pending/active routine (e.g. `kj@mangustacap.com`), Messa automatically cancels the prior routine without asking *"Do you want me to cancel the other one?"*.

---

### Pillar 2: Inbound Email Triage & Notification Gate
* **Problem**: In `messa/server.py`, Cloudflare email webhooks pushed promo emails (e.g. Metricool at 5:51 AM) straight to SMS.
* **Architecture: Three-Tier Inbound Filter**:
  ```
                  Incoming Inbound Email Webhook
                                 │
                                 ▼
             [Deterministic Mute Check (Pillar 3)]
                   │ Matches Mute Rule?
                   ├── YES ──► Silently Archive ($0 Token Spend)
                   └── NO
                                 │
                                 ▼
                 [Lightweight Priority Classifier]
                                 │
           ┌─────────────────────┼─────────────────────┐
           ▼                     ▼                     ▼
       [Tier 1: VIP]     [Tier 2: Morning Brief]  [Tier 3: Weekly]
    Direct human email,     Receipts, updates,     Marketing, promos,
    pitch replies, urgent    standard newsletters  discounts, sales
           │                     │                     │
           ▼                     ▼                     ▼
      Instant SMS            Rolled into 7 AM       Summarized on
     Notification           Daily Briefing text     Sunday evening
  ```

---

### Pillar 3: Dynamic User Policy & List Store
* **Problem**: When told to mute Metricool, Messa had no list store and created a 30-minute recurring cron job that queried the database 48 times a day.
* **Architecture**: Deterministic declarative database tables (`user_lists` and `user_policies`):
  ```sql
  -- Migration 038: Dynamic User Policies & Lists
  CREATE TABLE IF NOT EXISTS user_lists (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      list_name VARCHAR(100) NOT NULL, -- 'muted_email_senders', 'vip_contacts', 'preferred_airlines'
      item_value TEXT NOT NULL,
      metadata JSONB DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE(user_id, list_name, item_value)
  );
  CREATE INDEX IF NOT EXISTS ix_user_lists_lookup ON user_lists(user_id, list_name);

  CREATE TABLE IF NOT EXISTS user_policies (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      event_trigger VARCHAR(100) NOT NULL, -- 'on_inbound_email', 'on_schedule_meeting'
      condition_expression JSONB NOT NULL,
      action_type VARCHAR(100) NOT NULL,   -- 'silent_archive', 'defend_boundary', 'enforce_sender'
      description TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
  ```
* **Tools**: `manage_user_list(action, list_name, item)` and `set_user_policy(event, condition, action)`.
* **Zero-Token Check**: Inbound webhook verifies `is_item_in_user_list(user_id, 'muted_email_senders', domain)` in 1ms before invoking any LLM.

---

### Pillar 4: Latency & Subagent Execution Optimization
* **Problem**: Multi-part turns took up to **6m 43s** due to serial subagent loops (`routines_agent` $\to$ `orchestrator` $\to$ `email_agent`).
* **Architecture**:
  * **Parallel Tool Invocation**: Dispatch disjoint subagents concurrently via `asyncio.gather()`.
  * **Short-Circuit Empty Skills**: Bypass `search_skills` tool loops if domain skill count is 0.
  * **Fast-Path Draft Preview**: When user asks to preview a draft in their own inbox, fire via standard SES/Resend client directly without full subagent planning.

---

## 3. The 6 Senses of the Monster Autonomous Agent

```
               ┌──────────────────────────────────────────────┐
               │         MESSA V3 AUTONOMOUS MIND             │
               └──────────────────────┬───────────────────────┘
                                      │
        ┌──────────────┬──────────────┼──────────────┬──────────────┬──────────────┐
        ▼              ▼              ▼              ▼              ▼              ▼
    [SENSE 1]      [SENSE 2]      [SENSE 3]      [SENSE 4]      [SENSE 5]      [SENSE 6]
   Pre-Cognitive  Relationship   Circadian &    Real-World     Pre & Post     Zero-Shot
    Shadow Context    Radar       Cognitive      Browser        Meeting        Policy
    Anticipation   & Chameleon    Attention       Proxy        Dossiers &     Guardrail
     (Dominoes)       Voice        Budget      (Stagehand)     Harvesting      Defense
```

### SENSE 1: Pre-Cognitive Shadow Context & Domino Calculation
* **Travel Conflict Domino**: Catches a 4:15 PM SFO arrival conflicting with a 4:30 PM downtown meeting and proactively offers to push it before the user even realizes.
* **Commitment Ledger**: Silently tracks promises made in messages (*"I'll send the updated deck by Thursday"*) and nudges the user with a ready-made draft on Wednesday afternoon.
* **Receipt Auto-Reconciliation**: Automatically matches Uber/hotel receipts to active expense folders without buzzing the user.

### SENSE 2: Social Graph & Tone Chameleon (Relationship Radar)
* **Implicit Relationship Hierarchy**: Automatically distinguishes `INVESTOR`, `CLIENT`, `TEAM`, `FAMILY`, and `VENDOR`.
* **Tone Mirroring**: Adapts an informal shorthand dictation (*"yo tell mark funds arrived"*) into institutional prose for VCs and celebratory warmth for co-founders.
* **Dormant Network Keeper**: Alerts the user when a high-value relationship has gone cold for 45+ days with a relevant reason to reconnect.

### SENSE 3: Circadian Rhythm & Cognitive Attention Budgeting
* **Quiet Confidence**: Detects deep work or meetings from the calendar and suppresses non-critical alerts into a silent queue.
* **Dynamic Compression**: Delivers military-grade brevity during busy weekdays (*"Done. Moved to 3pm."*) and high-level strategic recaps on Sunday evenings.

### SENSE 4: Autonomous Web Proxy & Real-World Execution (Stagehand)
* **Zero-Friction Bookings**: Finds dining reservations on Resy/OpenTable, holds the slot, and executes on confirmation via card-on-file.
* **Flight Check-in & Boarding Pass**: Automatically checks in 24 hours prior, picks preferred seating, and sends the Apple Wallet pass.
* **Delivery Watchdog**: Monitors package tracking exceptions and drafts refund inquiries for delayed deliveries.

### SENSE 5: Pre-Meeting Dossiers & Post-Meeting Harvesting
* **T-10 Min Briefing**: 3 crisp bullets on who you're meeting, what you discussed last, and what your objective is.
* **T+3 Min Audio Harvest**: Asks for quick voice notes immediately after calls and converts them into drafted follow-ups, intros, and CRM notes.

### SENSE 6: Zero-Shot Policy Imprinting & Boundary Defense
* **Reflex Imprinting**: Turning verbal corrections (*"Don't book calls before 10 AM"*) into permanent declarative rules.
* **Active Protection**: Defending user time against incoming calendar invites autonomously (*"Dave asked for 9:30 AM; protecting your gym boundary, I offered him 10:30 AM instead"*).

---

## 4. Autonomous Missions & Project Capsules (The Goal-Directed Engine)

The single biggest leap from an AI assistant to an **Autonomous Chief of Staff** is **The Project Capsule**: the ability to delegate an entire ongoing objective and walk away, trusting Messa to manage it across weeks until completion.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        PROJECT MISSION CAPSULE                         │
│  Goal: "Secure $850 Refund from Delta for Cancelled Flight DL1042"     │
│  Status: IN_PROGRESS (Cadence: Every 24h | Next Wakeup: Tomorrow 9 AM) │
├────────────────────────────────────────────────────────────────────────┤
│  VAULT (Isolated Context & Media):                                     │
│  • PNR / Ticket Number: 0062481920194                                  │
│  • Booking Confirmation PDF & Receipt                                  │
│  • Image: Photo of damaged luggage barcode (luggage_tag_0902.jpg)      │
│  • Target: $850.00 cash refund to original Amex (Refuse vouchers)      │
├────────────────────────────────────────────────────────────────────────┤
│  AUTONOMOUS WORKERS:                                                   │
│  ├── [Email Thread Manager]: Tracks Delta Customer Care case #849102   │
│  ├── [Regulatory Advocate]: Cites US DOT 14 CFR Part 260 refund law    │
│  └── [Cadence Clock]: Wakes up every 24h; escalates if silence > 72h   │
├────────────────────────────────────────────────────────────────────────┤
│  SURGICAL USER INTERROGATION (When missing keys arise):                │
│  SMS: "Delta offered $900 eCredit or $850 refund. Per your policy,    │
│       I'm holding out for cash. Reply 'voucher' if you prefer eCredit."│
└────────────────────────────────────────────────────────────────────────┘
```

### Architectural Principles of Project Capsules:

1. **Context Isolation (The Clean Vault)**:
   * Standard chatbots dump all conversation into one massive message history. When handling a complex task, this causes context poisoning, token bloat, and hallucinations.
   * **Project Capsules hold their own isolated state**:
     * Target Goal & Definition of Done (`success_criteria`).
     * Project Vault: documents, PDFs, photos, receipts, tracking numbers, vendor contacts.
     * Event Timeline: every outgoing email, vendor response, browse action, and decision.

2. **Self-Governing Cadences & Autonomous Clocks**:
   * A project is **active in the background** without the user needing to remember to ask.
   * Messa computes its own wakeup schedules:
     * *Example (Airline Refund)*: Wake up every 24 hours. Check inbox for reply to Case #849102. If no reply after 72 hours, automatically send a polite escalation draft citing DOT consumer refund rules.
     * *Example (Holiday Gift Hunt)*: Check Amazon/BestBuy for price drops on Sony WH-1000XM5 headphones every Tuesday and Friday morning.

3. **Multi-Modal Asset Vault**:
   * Users can dump photos and documents straight into the project via iMessage/MMS:
     * User: *"Here's a photo of the cracked screen and the FedEx label. Add it to the Apple repair project."*
     * Messa: Ingests image into `project_vault_assets`, extracts the serial number via vision, and attaches it to the ongoing support thread.

4. **Surgical Asynchronous User Interrogation**:
   * Messa never bothers the user with trivia, but when an impassable branch or settlement decision occurs, she reaches out with a razor-sharp, single-tap SMS question:
     > *"Delta customer care approved our claim, but they require the last 4 digits of the card used to book flight DL1042. Send me the 4 digits and I'll wrap up the refund."*
   * The user texts: *"4019"*
   * Messa captures the input into the project vault, sends the reply to Delta, confirms the refund, and updates the project status.

5. **Exemplar Project Archetypes**:

| Project Archetype | Goal & Trigger | Autonomous Capabilities Executed | Definition of Done |
|---|---|---|---|
| **The Customer Care Dispute** | *"Get my $350 refund from United for the 6-hour delay."* | Monitors email thread, cites DOT regulations, sends follow-ups every 48h, asks user for card details when approved. | Refund credited to card; confirmation email archived. |
| **The Family Holiday Gift Hunt** | *"Plan Christmas gifts for Dad, Mom, and Alex under $500 total."* | Stores gift ideas, monitors price drops across web stores via Stagehand, tracks ship-by deadlines, alerts when deals hit target price. | All gifts purchased and delivered before Dec 22. |
| **The Apartment / Office Lease Scout** | *"Find a 2-bedroom in Williamsburg under $4,500 with in-unit W/D."* | Scrapes listings on StreetEasy/Zillow, emails listing agents for open house slots, aggregates comparison table into Drive. | 3 vetted tours scheduled on user's calendar. |
| **The Pre-Seed Investor Pipeline** | *"Manage outreach to the 25 VCs from the SaaStr list."* | Personalizes outreach drafts, logs replies, auto-schedules Zoom calls on calendar, nudges warm leads who went silent after 5 days. | All 25 targets processed (meeting booked or passed). |

---

## 5. Master Schema: Dynamic Personalization & Project Engine

```sql
-- Migration 038: Dynamic User Policies, Lists, Commitments & Autonomous Projects

-- 1. Deterministic User Lists
CREATE TABLE IF NOT EXISTS user_lists (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    list_name VARCHAR(100) NOT NULL, -- 'muted_email_senders', 'vip_contacts', 'preferred_airlines'
    item_value TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, list_name, item_value)
);
CREATE INDEX IF NOT EXISTS ix_user_lists_lookup ON user_lists(user_id, list_name);

-- 2. Declarative Behavioral Policies & Guardrails
CREATE TABLE IF NOT EXISTS user_policies (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_trigger VARCHAR(100) NOT NULL, -- 'on_inbound_email', 'on_schedule_meeting', 'on_channel_select'
    condition_expression JSONB NOT NULL,
    action_type VARCHAR(100) NOT NULL,   -- 'silent_archive', 'defend_boundary', 'enforce_sender'
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_policies_trigger ON user_policies(user_id, event_trigger);

-- 3. Social Graph & Relationship Radar
CREATE TABLE IF NOT EXISTS contact_profiles (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email VARCHAR(255),
    phone VARCHAR(50),
    full_name VARCHAR(255),
    relationship_type VARCHAR(50) DEFAULT 'ACQUAINTANCE', -- 'INVESTOR', 'CLIENT', 'PROSPECT', 'TEAM', 'VIP'
    communication_tone VARCHAR(50) DEFAULT 'PROFESSIONAL', -- 'CASUAL', 'DIRECT', 'FORMAL', 'CHAMELEON'
    last_interaction_at TIMESTAMPTZ,
    key_context JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, email)
);
CREATE INDEX IF NOT EXISTS ix_contact_profiles_lookup ON contact_profiles(user_id, email);

-- 4. The Commitment Ledger (Unspoken Promises)
CREATE TABLE IF NOT EXISTS user_commitments (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    counterparty_name VARCHAR(255),
    counterparty_email VARCHAR(255),
    commitment_summary TEXT NOT NULL,
    due_date DATE,
    source_type VARCHAR(50), -- 'INBOUND_EMAIL', 'OUTBOUND_SMS', 'MEETING_NOTE'
    status VARCHAR(50) DEFAULT 'PENDING', -- 'PENDING', 'FULFILLED', 'DISMISSED'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_commitments_user ON user_commitments(user_id, status);

-- 5. Autonomous Projects & Mission Capsules
CREATE TABLE IF NOT EXISTS user_projects (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_name VARCHAR(255) NOT NULL,
    category VARCHAR(100) DEFAULT 'GENERAL', -- 'DISPUTE', 'PURCHASE', 'RESEARCH', 'OUTREACH'
    goal_statement TEXT NOT NULL,
    success_criteria TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'ACTIVE',     -- 'ACTIVE', 'PAUSED', 'WAITING_USER_INPUT', 'COMPLETED', 'CANCELLED'
    check_cadence_cron VARCHAR(50),          -- e.g. '0 9 * * *' (Every morning at 9am)
    next_check_at TIMESTAMPTZ,
    associated_thread_id VARCHAR(255),       -- Email thread or external ticket id
    state_metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_projects_active ON user_projects(user_id, status);

-- 6. Project Vault (Multi-Modal Assets & Proofs)
CREATE TABLE IF NOT EXISTS project_vault_assets (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES user_projects(id) ON DELETE CASCADE,
    asset_type VARCHAR(50) NOT NULL,         -- 'IMAGE', 'PDF', 'RECEIPT', 'TRACKING_NUMBER', 'NOTE'
    file_url TEXT,
    extracted_text TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_project_vault_assets ON project_vault_assets(project_id);

-- 7. Project Execution Timeline (Immutable Audit Log)
CREATE TABLE IF NOT EXISTS project_timeline_events (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES user_projects(id) ON DELETE CASCADE,
    actor VARCHAR(50) NOT NULL,              -- 'MESSA_AGENT', 'COUNTERPARTY', 'USER'
    event_type VARCHAR(100) NOT NULL,        -- 'EMAIL_SENT', 'EMAIL_RECEIVED', 'USER_PROMPTED', 'BROWSER_ACTION', 'STATUS_CHANGED'
    summary TEXT NOT NULL,
    event_payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_project_events ON project_timeline_events(project_id);
```

---

## 6. Phased Implementation Roadmap

| Phase | Milestone | Core Deliverables | User Impact |
|---|---|---|---|
| **Phase 1** | **One-Touch Approvals & Conflict Auto-Supersede** | Update approval detection in `messa/turn_control.py` to auto-arm on affirmative phrases; auto-cancel conflicting recipient routines. | Eliminates 60% of redundant confirmation turns. |
| **Phase 2** | **Dynamic User Lists & Deterministic Mute Engine** | Migration 038 (`user_lists`, `user_policies`), `manage_user_list` tool, and 1ms zero-token webhook filter. | Instantly mutes promo spam without spinning up 30-min cron jobs. |
| **Phase 3** | **Three-Tier Inbound Email Triage** | Lightweight priority classifier: VIP $\to$ Instant SMS; Updates/Receipts $\to$ 7 AM Briefing; Marketing $\to$ Weekly digest. | Stops 5:51 AM notification fatigue. |
| **Phase 4** | **Subagent Concurrency & Latency Drop** | Parallel subagent execution (`asyncio.gather`), cache skills index, fast-path draft delivery. | Cuts response times from 6 minutes to under 15 seconds. |
| **Phase 5** | **Meeting Dossiers & Commitment Ledger** | 10-min pre-meeting SMS briefs, 3-min post-meeting voice harvest, implicit commitment tracker. | Transforms Messa into an executive chief of staff. |
| **Phase 6** | **Autonomous Project Capsules & Multi-Modal Vault** | Migration 038 project tables (`user_projects`, `project_vault_assets`, `project_timeline_events`), Project Orchestrator subagent, autonomous cadence scheduler, and surgical SMS check-ins. | Empowers users to delegate multi-week, multi-modal goals and walk away until completed. |

---

*This document is the official architectural master plan for Messa V3.*
