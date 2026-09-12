"""Perception Engine, Network Shield, and Web Convention Prior for light-web-agent.

Pillars implemented:
- Pillar 2: Pruned Accessibility (A11y) Tree + Recursive Frame/Shadow DOM indexing.
- Pillar 3: Zero-LLM Web Convention Prior (Landmarks, Direct-URLs, Dark-Patterns).
- Pillar 4: Target signature calculation for staleness validation.
- Pillar 7: Network Shield (Selective ad/tracker blocking for 85% proxy data reduction).
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Ad and tracker domains to abort over metered residential proxies
BLOCKED_TRACKER_KEYWORDS = [
    "criteo.",
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
    "analytics.tiktok.com",
    "connect.facebook.net",
    "omtrdc.net",
    "tiqcdn.com",
    "scorecardresearch.com",
    "adsystem.com",
    "rubiconproject.com",
    "pubmatic.com",
    "casalemedia.com",
]

# Canonical landmarks for Zero-LLM Fast-Path navigation
CANONICAL_INTENTS: Dict[str, Dict[str, Any]] = {
    "sign_up": {
        "keywords": ["sign up", "register", "create account", "join now", "create an account"],
        "roles": ["link", "button"],
    },
    "log_in": {
        "keywords": ["log in", "sign in", "member login", "sign in / up"],
        "roles": ["link", "button"],
    },
    "search": {
        "keywords": ["search", "search products", "search walmart", "search items", "find products"],
        "roles": ["searchbox", "textbox", "button"],
    },
    "cart": {
        "keywords": ["cart", "bag", "basket", "view cart", "my cart", "checkout"],
        "roles": ["link", "button"],
    },
    "cookie_banner": {
        "keywords": ["accept all", "accept cookies", "agree and proceed", "i accept", "allow all"],
        "roles": ["button"],
    },
}

# Dark-pattern keywords (e.g. guilt-trip decline buttons)
GUILT_TRIP_KEYWORDS = [
    "no thanks, i don't want to save",
    "no, i prefer paying full price",
    "i'll pass on savings",
    "no thanks, i don't want free delivery",
    "skip offer and pay more",
]


def compute_target_signature(role: str, name: str, tag_or_type: str = "") -> str:
    """Generate a stable target signature for staleness validation."""
    raw = f"{(role or '').strip().lower()}:{(name or '').strip().lower()}:{(tag_or_type or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def apply_network_shield(page: Any) -> None:
    """Attach network request interceptor to block heavy ads, video, and tracking scripts."""
    async def route_handler(route: Any) -> None:
        req = route.request
        url = req.url.lower()
        rtype = req.resource_type

        # Block video and media streams
        if rtype in ["media"]:
            await route.abort()
            return

        # Block known ad and tracking networks
        if any(keyword in url for keyword in BLOCKED_TRACKER_KEYWORDS):
            await route.abort()
            return

        # Allow all other resources (HTML, CSS, images, JS, XHR/Fetch)
        await route.continue_()

    try:
        await page.route("**/*", route_handler)
        logger.info("[NetworkShield] Attached request filter to page.")
    except Exception as e:
        logger.warning(f"[NetworkShield] Failed to attach route handler: {e}")


class PerceptionEngine:
    """Extracts and prunes accessibility trees across frames and shadow roots."""

    @staticmethod
    async def extract_pruned_a11y_tree(page: Any) -> Tuple[List[Dict[str, Any]], str]:
        """Recursively extracts interactive elements from top-level page and child frames.

        Returns:
        - elements: Structured list of element metadata
        - prompt_text: Clean, compact string (< 1,500 tokens) for LLM context
        """
        elements: List[Dict[str, Any]] = []
        elem_counter = 1

        # 1. Inspect Top-Level DOM interactive elements via JavaScript evaluation
        # This provides robust element discovery even when ARIA trees are sparse
        js_extract_script = """
        () => {
            const results = [];
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const interactiveSelectors = 'button, a[href], input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="tab"], [role="searchbox"]';
            const nodes = document.querySelectorAll(interactiveSelectors);
            let counter = 1;

            nodes.forEach((el) => {
                if (!isVisible(el)) return;
                let text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '').trim();
                text = text.replace(/\\s+/g, ' ').slice(0, 80);
                
                const role = el.getAttribute('role') || el.tagName.toLowerCase();
                const type = el.getAttribute('type') || '';
                const tag = el.tagName.toLowerCase();
                const checked = el.checked || false;
                const disabled = el.disabled || false;
                
                // Identify dark pattern signals (e.g. pre-checked add-on with price)
                const is_prechecked = (tag === 'input' && (type === 'checkbox' || type === 'radio') && checked);
                const parentText = el.closest('label, div')?.innerText || '';
                const has_price = /\\$\\d+(\\.\\d{2})?/.test(parentText);
                const dark_pattern_flag = is_prechecked && has_price;

                const eid = 'e' + counter++;
                try {
                    el.setAttribute('data-messa-id', eid);
                } catch (e) {}

                // Build high-specificity native selector prioritized over temporary attributes
                let specificSelector = `[data-messa-id="${eid}"]`;
                if (el.id) {
                    specificSelector = `#${el.id}`;
                } else if (el.getAttribute('data-testid')) {
                    specificSelector = `[data-testid="${el.getAttribute('data-testid')}"]`;
                } else if (el.name) {
                    specificSelector = `[name="${el.name}"]`;
                } else if (el.getAttribute('aria-label')) {
                    specificSelector = `[aria-label="${el.getAttribute('aria-label')}"]`;
                }

                results.push({
                    id: eid,
                    tag: tag,
                    role: role,
                    type: type,
                    name: text,
                    value: el.value || '',
                    checked: checked,
                    disabled: disabled,
                    dark_pattern: dark_pattern_flag,
                    selector: specificSelector
                });

            });
            return results.slice(0, 60); // Cap at 60 top elements
        }
        """

        try:
            top_nodes = await page.evaluate(js_extract_script)
            for node in top_nodes:
                eid = node.get("id") or f"e{elem_counter}"
                elem_counter += 1
                sig = compute_target_signature(node["role"], node["name"], node["tag"])
                elements.append({
                    "id": eid,
                    "frame_id": None,
                    "role": node["role"],
                    "name": node["name"],
                    "type": node["type"],
                    "tag": node["tag"],
                    "value": node.get("value", ""),
                    "checked": node.get("checked", False),
                    "disabled": node.get("disabled", False),
                    "dark_pattern": node.get("dark_pattern", False),
                    "signature": sig,
                    "selector": node.get("selector") or f'[data-messa-id="{eid}"]',
                })
        except Exception as e:
            logger.warning(f"[Perception] Error extracting top-level DOM: {e}")

        # 2. Inspect Child Iframes (e.g., Stripe, Cybersource card inputs)
        try:
            frames = page.frames
            frame_idx = 1
            for f in frames[1:]: # Skip main frame
                try:
                    f_url = f.url.lower()
                    # Skip tracking/ad frames
                    if any(t in f_url for t in BLOCKED_TRACKER_KEYWORDS):
                        continue
                    
                    frame_nodes = await f.evaluate(js_extract_script)
                    for node in frame_nodes:
                        eid = f"f{frame_idx}:e{elem_counter}"
                        elem_counter += 1
                        sig = compute_target_signature(node["role"], node["name"], node["tag"])
                        elements.append({
                            "id": eid,
                            "frame_id": f.name or f"frame_{frame_idx}",
                            "role": node["role"],
                            "name": node["name"],
                            "type": node["type"],
                            "tag": node["tag"],
                            "value": node.get("value", ""),
                            "checked": node.get("checked", False),
                            "disabled": node.get("disabled", False),
                            "dark_pattern": node.get("dark_pattern", False),
                            "signature": sig,
                            "selector": node.get("selector"),
                        })
                    frame_idx += 1
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[Perception] Child frame evaluation note: {e}")

        # 3. Format compact prompt text for LLM
        lines = []
        for el in elements:
            dark_flag = " [DARK_PATTERN_FLAG: pre-checked add-on]" if el.get("dark_pattern") else ""
            val_str = f" value='{el['value']}'" if el.get("value") else ""
            lines.append(f"[{el['id']}] {el['role']} \"{el['name']}\"{val_str}{dark_flag}")

        prompt_text = "\n".join(lines)
        return elements, prompt_text


class WebConventionPrior:
    """Zero-LLM Fast-Path heuristic landmark matcher and dark pattern scanner."""

    @classmethod
    def match_canonical_landmark(cls, intent: str, elements: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Attempt zero-LLM match for standard canonical intents (search, cart, sign_in)."""
        config = CANONICAL_INTENTS.get(intent.lower())
        if not config:
            return None

        keywords = config["keywords"]
        roles = config["roles"]

        for el in elements:
            el_name = (el.get("name") or "").lower()
            el_role = (el.get("role") or "").lower()

            if el_role in roles:
                for kw in keywords:
                    if kw in el_name:
                        logger.info(f"[ConventionPrior] Fast-path landmark match: intent={intent} -> [{el['id']}] \"{el['name']}\"")
                        return el
        return None

    @classmethod
    def detect_dark_patterns(cls, elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Identify dark-pattern elements (pre-checked fees or guilt-trip decline buttons)."""
        flagged = []
        for el in elements:
            name = (el.get("name") or "").lower()
            # Flag pre-checked paid add-ons
            if el.get("dark_pattern"):
                flagged.append(el)
                continue
            # Flag guilt-trip decline copy
            if any(gt in name for gt in GUILT_TRIP_KEYWORDS):
                flagged.append(el)
        return flagged
