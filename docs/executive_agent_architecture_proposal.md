# Master Engineering & Product Specification
## Next-Generation Autonomous Executive Agent Architecture (Hermes 3, Grok & Claude Co-Work Standard)

---

### Executive Summary

This document specifies the target architecture for Messa's next-generation autonomous orchestrator and cognitive memory system. 

To deliver an assistant experience where delegating a task is as effortless and reliable as working with a human Chief of Staff, Messa departs from monolithic, flat-tool chat loops. Instead, Messa implements a **Hierarchical, Artifact-Centric Agentic Architecture** with a **Tripartite Cognitive Memory Fabric** inspired by state-of-the-art systems including Nous Hermes 3 / Letta, xAI Grok, and Claude Co-Work.

The core objectives of this architecture are:
1. **Zero Delegation Friction**: The user states an outcome; Messa figures out the steps, resolves missing parameters autonomously, and never leaks technical API plumbing onto the user.
2. **Permanent Cognitive Continuity**: High-value work products (spreadsheets, drafted contracts, research reports, calendar events) and personal preferences persist indefinitely with zero arbitrary cliff-decay.
3. **Execution Resilience**: Messa enforces automated fallback cascades and anti-loop circuit breakers, completely eliminating repetitive tool failures.
4. **Clean Air Interface**: User-facing replies are crisp, human, and outcome-first, translating system blockers into simple business choices.

---

### 1. The High-Level System Topology

Messa decouples inbound sensory processing, intent routing, cognitive reasoning, and background reflection into distinct layers:

```
                                  INBOUND SMS / iMESSAGE
                                             │
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 1: PERIPHERAL SENSORY SUBSYSTEM                           │
│                      (Google Gemini 2.5 Flash / Vision & Audio)                        │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ • Inbound Image Understanding & Document OCR.                                          │
│ • Voice Memo Transcoding (.caf -> .m4a) & High-Fidelity Audio Transcription.           │
│ • Delivers pure text & media metadata to downstream orchestrator.                      │
└────────────────────────────────────────────┬───────────────────────────────────────────┘
                                             │ Clean User Directive
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 2: FAST INTENT & ROUTING TRIAGE                           │
│                             (Sub-Second Classifier Gate)                               │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ • Fast categorization: Chit-chat / Quick Query / Deep Research / App Action / Calendar │
│ • Prevents tool clutter: Activates only the relevant subagent domain tools.            │
└────────────────────────────────────────────┬───────────────────────────────────────────┘
                                             │
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                       LAYER 3: CENTRAL REASONING ORCHESTRATOR                          │
│                    (DeepSeek V4 Pro / Claude 3.5 Sonnet / GPT-4o)                      │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ • Ambiguity & Clarification Probe: Asks 1-sentence, 1-tap questions if choices exist.  │
│ • Execution Guardrail: Enforces tool-first action; never narrate future work in text.  │
│ • Hierarchical Subagent Coordinator: Orchestrates Deepsearch, Integrations, and Email. │
└─────────────────────┬──────────────────────────────────────────────────┬───────────────┘
                      │ Reads Context & Pinned Assets                    │ Writes Outcomes
                      ▼                                                  ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        LAYER 4: COGNITIVE MEMORY FABRIC (ACMF)                         │
│                    (Core Profile + Asset Registry + Procedural Skills)                 │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ • Core Profile (Hot): User traits, VIP contacts, permanent preferences.                │
│ • Workspace Asset Registry: Persistent store of all Google Sheets, PDFs, links, events.│
│ • Procedural Playbooks (agent_skills): Learned domain recipes and anti-patterns.       │
└────────────────────────────────────────────┬───────────────────────────────────────────┘
                                             │ Post-Turn Async Pipeline
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│               LAYER 5: ASYNCHRONOUS "SLEEP & DREAM" CONSOLIDATION LOOP                 │
│                        (Self-Updating, Zero User Latency)                              │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ • Runs post-turn in background (0ms impact on iMessage delivery speed).                │
│ • Extracts new facts, resolves contradictions, updates entity graph and assets.        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 2. Layer 3: The Plan, Clarify & Execute Engine

Modern executive agents are neither purely "plan-first" (rigid waterfall) nor "blind tool-callers" (uncontrolled ReAct). They use an adaptive, self-healing execution loop:

```
                  ┌─────────────────────────────────────┐
                  │          Inbound Directive          │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
                  ┌─────────────────────────────────────┐
                  │    Ambiguity & Feasibility Probe    │
                  └──────────────────┬──────────────────┘
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
            [Ambiguity / Choice]            [Deterministic Task]
                     │                               │
                     ▼                               ▼
       ┌───────────────────────────┐   ┌───────────────────────────┐
       │   Low-Friction Question   │   │     Autonomous Plan       │
       │  (1-sentence + choices)   │   │  (Decomposed Milestones)  │
       └───────────────────────────┘   └─────────────┬─────────────┘
                                                     │
                                                     ▼
                                       ┌───────────────────────────┐
                                       │   Execution & Tool Loop   │
                                       │ (Silent ReAct Waterfall)  │
                                       └─────────────┬─────────────┘
                                                     │
                                                     ▼
                                       ┌───────────────────────────┐
                                       │ Log to Persistent Assets  │
                                       │  (Direct Link & Summary)  │
                                       └─────────────┬─────────────┘
                                                     │
                                                     ▼
                                       ┌───────────────────────────┐
                                       │ Concisely Report Outcome  │
                                       └───────────────────────────┘
```

#### A. The Clarification Protocol (When to Ask vs. When to Assume)
* **When to Ask**:
  * High-consequence or irreversible actions (sending cold outreach emails, deleting calendar events, financial purchases).
  * Strategic preference forks (e.g. *"I found 50 angels. Should I focus strictly on AI specialists ($500k checks) or broaden to general SaaS?"*).
* **How to Ask**:
  * Always format as a **1-sentence question with 2–3 clear, tappable multiple-choice options**.
  * Never assign open-ended homework or demand technical configurations from the user.
* **When to Assume & Act Autonomously**:
  * Formatting, data curation, search iterations, document styling, and app selection.
  * If a user says *"Put this in a spreadsheet"*, default immediately to Google Sheets without asking whether they prefer Excel, CSV, Airtable, or Notion.

#### B. The Execution Guardrail (Eliminating the "Empty Promise" Bug)
* When an unconstrained chat model generates text filler (*"I'm on it! I'm creating your Airtable base..."*), the model framework often treats the text as the final turn output and stops before calling the tool.
* **The Rule of Action Precedence**: The orchestrator is strictly forbidden from describing future or in-progress work in user-facing text within the same step it plans to invoke a tool. The tool call must execute first; only upon receiving real tool observations may Messa draft the response.

---

### 3. Layer 4: The Cognitive Memory Fabric (ACMF)

Rather than treating memory as an ephemeral rolling calendar window that causes sudden amnesia after an arbitrary number of days, Messa implements a multi-tier memory architecture structured by **Salience, Utility, and Persistence**.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               TIER 0: WORKING MEMORY                                   │
│                        (In-Flight Turn / Active Scratchpad)                            │
│  - Active task variables (current URL, candidate options, temporary drafts).          │
│  - Lives strictly during turn execution; committed or cleared on task completion.      │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Task Completes -> Asset Ingestion
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        TIER 1: PERSISTENT WORKSPACE ASSET REGISTRY                     │
│                        (`user_assets`: Resources That Never Expire)                    │
│  - Every Google Sheet, PDF contract, and event is stored as a first-class entity.      │
│  - Top 5 most active/recent assets are ALWAYS pinned into the hot context prompt.      │
│  - Older assets are retrieved Just-In-Time (JIT) the second the topic is mentioned.    │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Nightly / Async Consolidation Loop
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                          TIER 2: SEMANTIC & ENTITY GRAPH                               │
│                         (Archival Knowledge & Workspace Store)                         │
│  - People Graph: VIPs, colleagues, relationships, preferred contact emails.            │
│  - App & Workspace Config: Auto-memorized Workspace IDs, base IDs, and Drive folders.  │
│  - Project Knowledge: Ongoing multi-week initiative boards (e.g. Fundraising Round).   │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Problem Solved / Failure Corrected
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                          TIER 3: PROCEDURAL MEMORY                                     │
│                           (Skills & Playbooks Engine)                                  │
│  - "How to do things" (learned workflows, anti-patterns, site workarounds).            │
│  - Automatically injected into subagents so Messa never repeats a past failure.        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

#### A. The Persistent Workspace Asset Registry (`user_assets` Table)
Every generated file, external document, spreadsheet, and calendar event is a first-class citizen:
* `id`: Serial Primary Key
* `user_id`: Foreign key to `users`
* `asset_type`: `google_sheet` | `pdf_document` | `calendar_event` | `email_thread`
* `title`: Human-readable name (e.g. `"Angel Investors"`)
* `external_id`: Platform ID (e.g. Google Drive Spreadsheet ID `1u1CR-_VC...`)
* `url`: Direct accessible link
* `summary`: 1–2 sentence structured recap + key metadata points (e.g. *"Contains 10 angel investors: Elad Gil, Cyan Banister, Charlie Cheever with check sizes and locations."*)
* `last_referenced_at`: Timestamp (updated whenever the asset is accessed, modified, or discussed)

**Operational Impact**:
When the user texts hours, days, or weeks later: *"Send an email to those angel investors we found earlier"*, Messa reads the Asset Registry, extracts the 10 names from the asset summary, and drafts the email immediately—without searching Drive or asking the user for URLs.

#### B. The Entity & Workspace Graph (Eliminating Developer Jargon)
* **Connected App Metadata Cache**: When an app is connected or an account authorized, technical identifiers (Base IDs, Folder IDs, Workspace IDs) are stored in `user_app_entities`. When the user requests an action in Airtable or Drive, Messa resolves the parameters internally.
* **The People Graph (`people` table)**: Continuously records contacts, relationships, and email addresses mentioned in context (e.g. *"Krupa Anna = brother/VIP"*, *"Elad Gil = angel investor"*). Messa never asks for an email address she has encountered before.

#### C. Asynchronous "Sleep & Dream" Memory Consolidation
* Runs as a non-blocking background worker immediately following response dispatch:
  1. **Contradiction Resolution**: If a user updates a preference (*"I'm switching to non-stop flights only"*), the worker marks conflicting older records as `superseded_at = NOW()`.
  2. **Asset Upsert**: Extracts newly generated links and documents into `user_assets`.
  3. **Zero Latency**: User receives SMS replies within seconds; memory consolidation happens silently in the background.

---

### 4. Layer 5: Autonomous Resilience & Human-Grade Tool Mastery

To make the assistant feel like a real human Chief of Staff rather than brittle software, Messa enforces three operational rules:

#### A. The "Rule of 3 Fallbacks" (Self-Healing Execution)
When executing an action that requires storing or sharing data, Messa follows an autonomous waterfall before ever alerting the user:
1. **Primary Tool**: Dedicated integration (e.g. Airtable API).
2. **Automatic Fallback**: Connected universal workspace (e.g. Google Sheets / Drive).
3. **Immediate Direct Delivery**: Formatted PDF or clean Markdown table sent over text.
*Only if all three fail does Messa pause, presenting the user with a single, clear multiple-choice resolution.*

#### B. Anti-Loop Circuit Breaker
* If any tool action fails twice with identical or similar error signatures, Messa triggers an **Execution Breakout**:
  * She is strictly forbidden from making a 3rd identical attempt.
  * She is strictly forbidden from asking the user for developer credentials or workspace IDs.
  * She automatically pivots to the next tier in the fallback waterfall.

#### C. Proactive Context Re-Use
* Messa actively cross-references recently created assets with inbound instructions. If a user says *"Email these investors to my Gmail"*, Messa:
  1. Pulls the investor names from the active asset.
  2. Resolves the user's personal Gmail from `users.email`.
  3. Stages the email with real values populated and requests one-tap approval.

---

### 5. World-Class Operating Standards: The "Clean Air" Interface

To uphold executive-grade communication, all user-facing interactions adhere to the **Clean Air Interface**:

1. **Zero Technical Leaks**: Messa is strictly forbidden from mentioning HTTP status codes, JSON errors, Workspace IDs, API tokens, Composio, database schemas, or raw stack traces.
2. **Translate Blockers into Solutions**:
   * *Forbidden*: *"One holdup on Airtable: I hit a password wall or need your workspace ID starting with wsp."*
   * *Executive Standard*: *"Airtable needs an initial setup, so to save you time I’ve organized all 10 angel investors in this Google Sheet instead: [link]."*
3. **Outcome-First iMessage Cadence**:
   * Lead with the accomplished result in sentence 1.
   * Provide necessary links or details in sentence 2.
   * Consolidate outbound messages into a single cohesive turn—never spam consecutive introductory texts.

---

### 6. Recommended Implementation Roadmap

| Phase | Milestone | Deliverables |
|---|---|---|
| **Phase 1** | **Cognitive Memory & Asset Registry** | • Implement `user_assets` table for first-class persistent resources.<br>• Inject active hot assets into orchestrator context.<br>• Deploy async "Sleep & Dream" extraction worker. |
| **Phase 2** | **Orchestrator Realignment** | • Lock `MESSA_MODEL` to premier reasoning model (`deepseek-v4-pro` / Claude 3.5 Sonnet / GPT-4o).<br>• Preserve Gemini 2.5 Flash strictly for the `media_understanding.py` sensory pipeline. |
| **Phase 3** | **Execution Guardrails & Anti-Loop** | • Implement execution breakout circuit breaker.<br>• Deploy automated Rule of 3 fallback cascade (Airtable $\to$ Google Sheets $\to$ Direct PDF).<br>• Enforce strict prohibition on API jargon in system prompts. |
| **Phase 4** | **Entity & Workspace Graph** | • Implement auto-discovery cache for connected app workspace/folder IDs.<br>• Auto-enrich `people` table with relationships and communication habits. |
| **Phase 5** | **Project Boards** | • Implement multi-week project tracking boards for high-level user initiatives (Fundraising, Travel, Hiring). |
