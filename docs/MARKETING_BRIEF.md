# Messa — Marketing Brief

*Living reference for anyone (or any Claude project) writing marketing, copy, or positioning for Messa. Keep this file, and only this file, in context for marketing work — it's kept current by hand whenever a real feature or messaging decision changes, so it should never go stale silently. Last updated: 2026-09-04.*

## One-line description

Messa is an executive personal assistant that lives entirely inside your text messages — no app, no portal, no login. You text her like a person; she coordinates a team of specialist AI agents behind the scenes to actually get things done across your email, calendar, tasks, and the wider web.

## Positioning (from the live landing page)

Headline: **"The executive personal assistant that lives in your texts."**

Core pitch: Messa is available directly over text (iMessage, SMS, and RCS — she auto-falls-back across those so it always just works). She comes with a dedicated email address of her own, direct access to 1,400+ connected apps (Gmail, Google Calendar, Slack, Notion, and more via Composio), and autonomous web browsing to research, schedule, and execute tasks. An integrated purchasing card for authorized transactions is coming soon. The framing to lean on: **"Not a surface-level chatbot. A coordinated multi-agent system working behind every message."**

Contrast line that tests well: **"Advisory chatbots suggest. Messa executes."**

Onboarding promise: **"Onboarded in a single text."** No install, no account setup, no portal — save the contact and start texting.

## Target audience

Busy professionals / executives who want delegation, not another app to check. The existing landing page is written explicitly in "executive daily operations" language — think someone who'd otherwise hire an EA, not a general consumer productivity crowd. Keep copy in that register: capable, calm, high-trust — not gimmicky or overly casual.

## What Messa actually does (grounded in the real product, not aspirational)

- **Texts like a person, works like a team.** One conversation with "Messa" — behind it, an orchestrator delegates to specialist agents for research, email, documents, scheduling, and integrations, then reports back in plain language.
- **Her own email address.** Every user gets a real inbox on Messa's own domain (`<name>@textmessa.com`), separate from their personal Gmail. Messa can send, receive, and manage threads from it, sign her own outgoing mail with a "Messa" signature and a tagline inviting people to text her directly, and she's built with technical safety limits so two Messa-run inboxes emailing each other can never spiral into an infinite auto-reply loop — she'll always hand a long back-and-forth to a human after a couple of automatic turns.
- **Reads and manages the user's real Gmail too**, via a secure connected-account flow (OAuth) — searching, reading, sending, and reading attachments — kept as a completely separate path from her own inbox, so "Messa's email" and "your Gmail" are never confused.
- **Deep, live web research.** A dedicated research agent that actually browses the web — navigating sites, filling forms, clicking through multi-step flows — not just search-snippet summarizing, with safety rails (domain allow-lists, confirmation before anything destructive/irreversible).
- **Document generation.** Produces polished, legally structured documents as PDFs on request — NDAs, consulting agreements, MSAs, SOWs, offer letters — plus executive reports and briefings. Also reviews contracts for risk (a contract-risk audit feature) with clear disclaimers that it's not legal advice.
- **Tasks, reminders, notes, and contacts** handled natively and instantly (no confirmation friction for these), with calendar scheduling going through an explicit confirm step — reflecting the broader "ask, don't assume" design philosophy below.
- **1,400+ app integrations** via Composio (Gmail, Google Calendar, Outlook, Slack, Notion, Todoist, Asana, ClickUp, and far more) — Messa can connect to whichever app a user already lives in rather than forcing them into new tools.
- **Recurring automation.** Set-and-forget routines: recurring reminders, scheduled digests, deadline-aware follow-ups, background watchers that check on something and report back later.
- **Persistent memory.** Messa remembers context across conversations, with genuine database-enforced privacy isolation between users' memories (not just an app-level promise) — a real trust point if it comes up.

## Trust and safety themes (use these — they're real, not just claims)

- **Explicit confirmation for anything consequential.** Messa never unilaterally commits the user to something — she won't confirm a meeting time, spend money, or make a promise on the user's behalf without asking first. If someone emails proposing "let's meet next week," Messa relays the proposal and waits for the user's decision rather than deciding for them.
- **Real-time execution visibility** — the user can see what Messa is doing as she does it, not just a final answer.
- **Loop-safety by design**, not just policy — technical limits prevent runaway automated back-and-forths (e.g., two AI-run inboxes emailing each other).
- **Clear boundaries between "Messa's own" vs. "the user's real" accounts** (her own email address vs. Gmail; her own internal task list vs. a connected calendar/task app) — nothing gets silently merged or confused.

## Voice and tone

Confident, capable, understated — an executive assistant's voice, not a hype-y consumer app. Short, declarative sentences work well ("Onboarded in a single text." / "Advisory chatbots suggest. Messa executes."). Avoid over-promising autonomy — the honest, differentiated story is *delegation with judgment*, not *a robot that does whatever it wants*.

## What NOT to claim

- Don't imply full autonomy over money, scheduling, or anything consequential without the user's sign-off — that's explicitly against how Messa is designed to behave.
- Don't call document/contract features "legal advice" — they carry disclaimers on purpose.
- Don't conflate the free-tier SMS limitations (shared number, contact must text first, no cold outbound yet) with the general product pitch unless writing genuinely technical/setup content.

## Maintenance note

This file should be updated whenever a feature, safety behavior, or public positioning line materially changes (e.g., new integrations, new agents, changes to the autonomy/confirmation rules, a landing page rewrite). It intentionally does not include implementation detail, code, or internal architecture — just what's true and worth saying to a customer.
