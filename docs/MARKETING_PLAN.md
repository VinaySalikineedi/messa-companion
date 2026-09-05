# Messa — Marketing Plan (Private Beta Phase)

*Companion to MARKETING_BRIEF.md — that file is the source of truth on what Messa is and how to talk about it; this file is the plan for what we actually do with that positioning over the next several weeks. Drafted 2026-09-05.*

## UPDATE 2026-09-05: fast-tracking the launch to ride the Instinct moment

Decision: given Instinct's viral run and the backlash it's currently taking over unauthorized actions and data retention, we're compressing the original 5–6 week runway below and pushing to get Messa in front of people **this weekend**, while the "an agent that asks before it acts" contrast is genuinely in the news cycle rather than a hypothetical. The lane structure and copy further down this doc (X pillars, Reddit communities, investor email) still all apply — what's changing is the timeline and the news hook, not the fundamentals.

**The honest tradeoff, so this is a decision and not just a scramble:** most of what makes a launch go viral can genuinely happen tomorrow — X, Reddit, LinkedIn, and Hacker News don't punish weekend timing nearly as hard as people assume, and riding a live news cycle is worth more than optimal timing. Product Hunt is the one real exception: its algorithm and audience are heavily weekday-weighted, and Saturday/Sunday are its two worst traffic days by a wide margin — a weekend PH launch would burn the one-shot novelty for a fraction of the eyeballs. So the plan below splits it: **go loud on X/Reddit/LinkedIn/HN this weekend to catch the news cycle now**, and hold Product Hunt for **Tuesday**, using this weekend's momentum (comments, a bigger waitlist, maybe some press pickup) as fuel for that day instead of launching cold.

**Two things to lock down before anything goes out — this matters more, not less, because we're being aggressive:**

1. **The card/purchasing feature isn't ready.** Don't imply it. The brief already has the right line for this — "an integrated purchasing card for authorized transactions is coming soon" — use that phrasing verbatim if it comes up, and don't let any copy imply Messa can already move money today.
2. **Verify the "she asks before she acts" claim is actually true of the current build, right now, before you center a launch on it.** The entire contrast with Instinct rests on Messa genuinely confirming before anything consequential. If there's any known gap between that and the brief's description, this is the weekend to find and fix it (or scope the claim down to what's true) — publicly staking your launch on a trust claim that then fails live would be far worse than Instinct's current backlash, because it's your one differentiator. Also worth a quick gut-check on waitlist/backend capacity in case this actually pops off.

### Platform ranking for this weekend (by realistic virality with ~24–48 hours of lead time)

1. **X — the best bet for tomorrow.** No weekday penalty, algorithm rewards engagement velocity regardless of day, and the Instinct story is actively circulating there right now, which means both a founder thread and direct replies into existing Instinct discourse can travel fast. This is where a real demo clip matters most.
2. **Reddit — nearly as good, if done as a person, not a launch.** r/artificial, r/singularity, and r/OpenAI are almost certainly already running threads about the Instinct backlash this week — a genuine, well-written comment or a "here's the alternative approach we've been building" post timed into that conversation is high-leverage newsjacking. r/SideProject and r/EntrepreneurRideAlong remain good for a founder story post any day of the week.
3. **LinkedIn — underused for this, and a strong fit.** Messa's actual target user (busy professionals/executives) lives on LinkedIn, not Reddit. A founder post drawing the same contrast, written in a more measured, professional register, can travel well there and reaches exactly the buyer, not just the tech-Twitter crowd.
4. **Hacker News — good, slightly weaker on a weekend, still worth doing.** Traffic is lower Saturday/Sunday than a weekday morning, but Show HN doesn't have PH's algorithmic weekday bias, and the Instinct story (prompt injection, autonomous transactions) is exactly the kind of technical postmortem HN readers engage with. If it doesn't catch fire this weekend, a second, differently-framed technical post (e.g., on the confirmation-gating architecture specifically) can go up Tuesday alongside Product Hunt.
5. **Product Hunt — hold for Tuesday.** Use the weekend to line up 5–10 beta users genuinely willing to comment that day, get the demo GIF and founder first-comment ready, and go in with whatever momentum (waitlist growth, X impressions, press) the weekend generated.
6. **Short-form video (TikTok/Reels/Shorts) — stretch goal, only if a demo clip can be produced fast.** A real screen-recording of texting Messa and watching a task complete is the single most shareable asset in this entire plan if there's time to cut one; if not, don't force it this weekend and revisit for the PH/HN day instead.

### Ready-to-post copy for this weekend

**X — founder thread (post Saturday or Sunday, whenever a real demo clip is ready to attach to post 1):**

> 1/ Everyone's watching Instinct raise $250M in weeks, then spend the next week explaining why it booked a $200 reservation nobody approved and kept emails after being disconnected.
>
> That's not a bug. It's the design: an agent authorized to act first and tell you after.
>
> 2/ We built Messa the other way. She lives in your texts too — no app, same idea — but she asks before anything consequential happens. Books, sends, buys: you confirm first, every time.
>
> 3/ "Advisory chatbots suggest. Messa executes." But executing with your sign-off, not instead of it. That's the whole bet.
>
> 4/ In private beta now. [waitlist link]

**X — standalone reply-bait post (good for jumping into existing Instinct threads directly, adapt per thread):**

> The Instinct stories this week are a pretty clean argument for "ask first, act second." We've been building exactly that — same text-message interface, but nothing consequential happens without you confirming it. Early access: [link]

**Reddit — r/artificial or r/singularity comment/post (adapt to the specific thread, don't copy-paste identically across subs):**

> This is basically the core design tradeoff with standing-authority agents: give an AI real access and real authority to act, and eventually it acts on something you didn't actually want. We've been building an alternative take on the same "AI assistant that lives in your texts" idea, but with the opposite default — she can draft, schedule, and even browse the web to get things done, but anything consequential (sending, booking, buying) waits for an explicit yes from you first. Not as flashy as full autonomy, but it means you're never finding out after the fact what she did. Early access if anyone wants to try it: [link]

**LinkedIn — founder post (more measured tone, written for the executive audience):**

> The AI assistant category had a strange week. One of the most funded new entrants proved, publicly, why "autonomous by default" is a scarier promise than it sounds — real users found it had booked things, sent emails, and retained data they'd explicitly disconnected, without being asked.
>
> I've been building Messa with the opposite assumption: an assistant that lives entirely in your texts, coordinates real work across your email, calendar, and connected tools — but never commits you to anything without asking first. Delegation, not surprise.
>
> If you've been curious about AI assistants but wary of handing over that much control, this is the alternative. In private beta now — link below if you'd like early access.

**Hacker News — Show HN title and opener:**

> Show HN: Messa – a text-message AI assistant that asks before it acts
>
> We built Messa as a multi-agent system reachable entirely over iMessage/SMS/RCS — no app. The interesting engineering problem for us wasn't getting an LLM to use tools, it was building a confirmation-gating layer that makes "ask before anything consequential" a hard architectural boundary rather than a prompt instruction an agent can talk itself out of. Happy to go deep on the orchestration/safety design in the comments — this felt like a timely thing to share given the last week's news about agents with standing authority to act.

### This weekend, hour by hour

**Today (Saturday):** verify the confirmation-gating behavior end to end, finalize a short demo clip if at all possible, get the waitlist page copy tight, line up 5–10 beta users willing to genuinely engage on launch day (for Tuesday's PH push, not this weekend), and post the first X thread once the demo clip is ready.

**Sunday:** post to LinkedIn and the Reddit communities, reply into any live Instinct discourse on X with the reply-bait line, keep engaging every comment personally on all platforms — the first 24 hours of engagement is what determines whether any of this actually spreads.

**Monday:** post the Hacker News Show HN (morning Pacific time gets the longest runway), keep the content cadence going on X, and finalize Product Hunt assets (tagline, gallery images, founder first comment) using whatever traction language the weekend generated.

**Tuesday:** launch Product Hunt, with the beta users from Saturday commenting early, and cross-post the PH link everywhere the weekend conversation happened.

## Where we are and what this plan covers

Messa is in private beta with a waitlist, not a public launch. That changes the job of marketing right now: the goal isn't to maximize traffic to a signup page yet, it's to build a warm, growing waitlist, start earning founder-led credibility on X and Reddit, and open investor conversations — so that when we do pull the trigger on a Product Hunt / Hacker News launch, it lands on an audience that already half-knows us instead of total strangers.

This plan runs three lanes in parallel (waitlist growth, content/community, investor outreach) and treats a Product Hunt/HN launch as the culminating event once the waitlist and story are strong enough to make that day count, rather than something to do this week.

The one narrative to keep consistent everywhere: **"Advisory chatbots suggest. Messa executes."** She lives in your texts, no app or login, and behind every message a coordinated team of AI agents actually does the work — email, calendar, research, documents, 1,400+ connected apps — with explicit confirmation before anything consequential. That combination (real execution + real guardrails) is the whole pitch, on X, on Reddit, and to investors.

## Sequencing (roughly 5–6 weeks to a launch-ready state)

Weeks 1–2 focus on warm-up: get the X account posting in Messa's voice, start showing up genuinely in relevant Reddit communities, and get the waitlist page sharing-ready. Weeks 2–4 add investor outreach once there's some early traction (waitlist count, a few real usage stories) to point to, since "pre-anything" cold emails to investors convert far worse than "we have N people waiting and here's what they're saying." Week 5 (or whenever the waitlist and story are ready — don't force a date) is Product Hunt and/or a Show HN post, prepped like a real event with assets and a first-comment story ready in advance.

This is a plan to adjust, not a schedule to follow blindly — if a Reddit post or an investor reply gives us signal that changes the story, we update the plan, not just the tactics.

## Lane 1: Waitlist growth

The waitlist page itself is the first marketing asset — before driving traffic anywhere, it should have the headline and contrast line from the brief front and center ("The executive personal assistant that lives in your texts" / "Advisory chatbots suggest. Messa executes."), one line explaining onboarding is a single text, and ideally a short screen-recording or GIF of a real exchange with Messa (texting her a real task and showing her actually do it — this is Messa's single best marketing asset because the whole pitch is hard to believe until you see it).

A few low-cost ways to grow it during beta: ask every beta user directly for one specific favor — forward the waitlist link to one person who'd want an EA, or post their own experience if they had a good one (people trust a peer's post far more than ours); a short "why I'm building Messa" founder post pinned on X and cross-posted where relevant, ending with the waitlist link; and a simple referral angle once there's enough waitlist volume to make it worth building ("skip the line by sharing this link" is a cheap justification for a plain referral counter, even a manual one at this stage).

## Lane 2: X (Twitter)

**Voice on X should match the brief's tone** — confident, capable, understated, short declarative sentences — not hype-y startup Twitter voice. Avoid overclaiming autonomy; the honest differentiated story is delegation with judgment.

Content pillars to rotate through:

- **Demos.** Screen recordings or clip-style threads of Messa actually doing something end-to-end from a text message — this format alone can carry the account. Real tasks land better than staged ones: booking something, drafting and sending a document, triaging an inbox, chasing down an email thread.
- **Build-in-public.** Short, specific notes on what shipped this week, phrased as capability, not internal architecture ("Messa can now review a contract for risk before you sign it" rather than implementation detail).
- **Point of view on the category.** Contrast posts against "just another chatbot" — this is where the "suggest vs. execute" framing does the most work, and where a founder can share an honest opinion on why most AI assistants stall out at suggestions.
- **Trust and safety as a feature, not a caveat.** Most competitors either overclaim autonomy or hide behind disclaimers. Messa's real confirmation-gating and loop-safety design is a genuine differentiator worth its own posts, not just a footnote.

Draft posts to start with:

> Texted Messa "draft an NDA for a contractor starting Monday, standard mutual terms." Four minutes later: a PDF in my inbox, formatted, ready to send. No app. No portal. Just a text.

> Most "AI assistants" suggest. Ours executes.
> Messa doesn't just tell you what to do about that email — she drafts the reply, checks your calendar, and asks you to confirm before it goes out.

> The best AI assistant should feel like hiring someone, not downloading something. That's the whole design constraint behind Messa: onboarded in a single text, no install, no login.

> A thing I didn't expect to have to build: loop-safety. Two AI-run inboxes emailing each other can spiral forever if you're not careful. Messa hands any long back-and-forth to a human after a couple of automatic turns. Small detail, but it's the difference between "cool demo" and "thing I'd actually trust."

> Building Messa: an executive assistant that lives entirely in your texts. She has her own email address, reads 1,400+ connected apps, and browses the web to actually get things done — not summarize them. Early access waitlist open: [link]

Posting cadence: aim for consistency over volume — 3–5 posts a week is sustainable and enough to build a recognizable voice; a daily demo clip during weeks 1–2 would be a strong way to seed the account if there's footage to support it.

## Lane 3: Reddit

Reddit culture punishes anything that reads as an ad, so the rule here is value or a genuine story first, product link second (and often not in the post at all — let it come up in a comment or in your profile). Self-promotion rules vary by subreddit and get enforced, so check each community's rules before posting and never post the identical copy across multiple subreddits at once.

Communities worth being genuinely present in: r/SideProject and r/EntrepreneurRideAlong (both are built for build-in-public posts and are the most tolerant of an early product being mentioned as part of a real story), r/artificial and r/AI_Agents (technically-minded audiences who'll actually engage with the multi-agent architecture angle), and r/productivity or r/ExecutiveAssistants (closer to the actual target user — busy professionals wanting delegation — but require the softest touch, since these skew toward "tool recommendations from a real user" rather than founder posts).

Two post formats tend to work without reading as promotional:

> **A story post** (r/SideProject style): "I got tired of AI assistants that only suggest things, so I built one that actually executes — over text message. Here's what it does and what I learned building the trust/safety layer." Lead with the interesting problem (how do you let an AI actually take action without it going off the rails?), mention Messa naturally, and be ready to answer real questions in the comments — that's where the actual credibility gets built.

> **A helpful answer**, not a post at all: when someone in r/productivity or similar asks "is there an AI assistant that can actually do X for me," answer their specific question honestly (including where Messa isn't the right fit) and mention it as one option among the honest tradeoffs. This converts better than any top-level post because it's answering a real need at the moment someone has it.

Plan to spend time in these communities before posting anything — commenting genuinely on other people's threads for a week builds the account history that makes a later post read as a real person, not a drive-by marketer.

## Lane 4: Investors

Do this once there's something concrete to point to — waitlist size, a real usage story, a specific number (even "N people signed up in the first week with zero paid spend" is a data point). A cold email with a strong product idea and zero traction is a much harder sell than the same idea with any traction attached.

The investor narrative is slightly different from the consumer pitch: lean into the multi-agent orchestrator as real technical infrastructure (routing, tool-use safety, session/state management across long-running tasks — the kind of engineering that's hard to replicate quickly), the category thesis (AI assistants are stuck at "suggest," and the shift to agents that can safely execute is the next real platform shift), and the distribution angle (SMS is zero-install, works on every phone, and the target user — busy professionals — is underserved by app-based productivity tools they won't open).

Draft cold outreach email:

> Subject: Messa — the AI executive assistant that lives in your texts
>
> Hi [Name],
>
> Most "AI assistant" products stop at suggestions. Messa doesn't — she's a coordinated multi-agent system that lives entirely inside text messages (iMessage/SMS/RCS, zero install) and actually executes: drafting and sending documents, managing a real inbox, running 1,400+ connected app integrations, and browsing the web to get things done, all gated behind explicit user confirmation for anything consequential.
>
> We're in private beta with [N] people on the waitlist and [a specific early signal — e.g., "growing without any paid spend" or a strong usage anecdote]. I think [Fund]'s work in [relevant thesis — AI agents / vertical AI / future of work] makes this a natural fit, and I'd love 20 minutes to show you Messa actually doing something live over text.
>
> Would [day/time] work?
>
> [Name]

Finding the right investor list is worth doing deliberately rather than blasting a generic list — targeting funds and angels with a stated thesis in AI agents, future-of-work, or prosumer productivity will convert far better than volume. If useful, a prospecting connector (there's one called Vibe Prospecting already available in this workspace) could help build a targeted contact list once you're ready to start outreach — happy to use it when we get there.

## Lane 5: Product Hunt / Hacker News launch (when ready, not yet)

Treat this as a single, well-prepared day rather than something to do casually — a weak Product Hunt or Show HN debut is hard to redo, since both platforms reward novelty.

**Product Hunt readiness checklist:** a tagline that fits the "suggest vs. execute" framing in under 60 characters; a 30–60 second demo GIF or video showing a real text-in, task-done-out exchange (this matters more than any copy on the page); a founder first comment ready to post the moment it goes live, telling the honest origin story and inviting questions; a handful of beta users lined up in advance who are genuinely willing to comment (not upvote-trade — PH's algorithm penalizes obvious vote manipulation); and a launch day itself where the founder is available all day to respond to every comment, since engagement in the first few hours is what the ranking algorithm rewards most. Tuesday–Thursday tends to have the most engaged PH audience; avoid major holidays and big competing launches if you can see them coming.

**Show HN readiness:** Hacker News has a stricter, more skeptical audience that punishes anything reading as a pitch. A good Show HN post title is plain and factual ("Show HN: Messa – an AI assistant you talk to entirely over text"), the post itself explains what it does and, ideally, something technically interesting about how (the multi-agent orchestration, the loop-safety design, the OTP-handling problem — these are exactly the kind of engineering details HN commenters respect), and the founder needs to be present in the comments for the first several hours answering technical questions directly and without defensiveness, including hard ones. Early morning Pacific time on a weekday tends to get the longest runway on the front page.

Both of these should launch on the same day if we do both, since a Product Hunt launch is a link worth mentioning on Hacker News (and vice versa) while the story is fresh.

## What I can and can't do directly from here yet

I can write and refine every piece of copy above, and keep this plan updated as things change. To actually publish posts to X, post to Reddit, or send investor emails from this account rather than handing you the drafts to send yourself, I'd need two things connected that aren't set up in this workspace yet: an email connector (Gmail, so I can send from your real address rather than just drafting) and a social scheduling connector (Metricool showed up as an available option here — it can post to X and other networks, though I'd want to confirm Reddit support specifically since Reddit's posting norms make manual, personally-voiced posts the safer choice anyway). Until either is connected, my plan is to keep producing ready-to-send drafts here and you copy/paste or forward them — which for Reddit specifically is genuinely the better approach anyway, since Reddit trusts a personally-posted account far more than anything that reads as scheduled or automated.

## Immediate next actions this week

1. Get the waitlist page reading with the headline/contrast line from the brief and, if possible, get one real demo clip recorded — this unlocks the single best asset for X, Reddit, and the eventual PH/HN launch simultaneously.
2. Post the first 2–3 X drafts above and start genuinely commenting in r/SideProject, r/artificial, and r/productivity threads (no posting yet — just building real presence).
3. Decide on the specific early traction number we'll use once we start investor outreach (waitlist count, a usage anecdote, or both), so Lane 4 has something concrete to open with.
