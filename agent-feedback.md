# Action Items for Team: Reliability Fixes & Smart Screenshot Sharing
**File:** `agent-feedback.md`  
**Status:** Required before merging `feature/scratchpad-and-skills` to `main`

---

## 1. Critical Blocker: Fix False-Positive in `browser_circuit_breaker.py`

* **Issue:** `StagehandZeroDeltaMiddleware` fingerprints only `(url, title)`. On Single Page Apps (Uber, Amazon checkout, Stripe), typing into form fields or clicking checkboxes **does not change the URL or title**.
* **Impact:** Any 3 sequential `browser_act` calls on the same page (e.g. First Name, Last Name, Email) trip `ZeroDeltaExceeded` and crash valid tasks on the 3rd field.
* **Fix:** In `messa/tools/browser_circuit_breaker.py`, only increment `self._streak` if the **action/instruction is identical** to the previous call, OR include a lightweight DOM content length (`await page.evaluate("document.body.innerText.length")`) in the fingerprint.

---

## 2. OTP Invalidation Fix: Prevent Early Delegation Return

* **Issue:** Deepsearch closed the browser in the live test not because it called `close_browser()`, but because it finished its delegation turn and returned to Messa, ending `async with provider:`. When Messa re-delegated with the code, a fresh session opened and invalidated the OTP.
* **Fix:** In `DEEPSEARCH_SYSTEM_PROMPT`, instruct deepsearch that when an OTP screen appears, it **must remain in the browser and call `await_email_verification_code` directly** rather than returning to Messa to ask `personal_inbox_agent`.

---

## 3. New Feature: Smart Screenshot Sharing (`send_screenshot`)

Equip `deepsearch` with a smart `send_screenshot(caption)` tool to text high-value visual proof to the user via MMS without spamming.

### Implementation:
* **Tool:** `send_screenshot(caption: str = "")`
* **Mechanism:** Takes `await page.screenshot()`, uploads to public asset store / Presigned S3 / Sendblue media, and sends via `sendblue.send_message(user.phone_number, text=caption, media_url=url)`.

### "Zero-Spam" Smart Rules (Enforced in System Prompt):
1. **High-Value Milestones Only:**
   * Cart built with selected items and prices.
   * Final confirmation / receipt / form submission page.
   * Account created and user landing dashboard.
2. **Long-Running Reassurance:**
   * If an in-depth browsing task takes >90–120 seconds, send a progress screenshot with a short note explaining current status.
3. **Blockers & Human-in-the-Loop:**
   * When hitting a CAPTCHA, 2FA screen, or ambiguous selection where human eyes are helpful.
4. **Strict Ban on Micro-Action Spam:**
   * Strictly forbidden from sending screenshots for intermediate steps (typing fields, clicking ordinary buttons, scrolling).
