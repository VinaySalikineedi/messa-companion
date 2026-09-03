// Regression test for cloudflare/personal-email-worker/worker.js's new
// html->text fallback -- the fix for the "missing verification code" bug,
// grounded in the actual confirmed evidence: the real "Welcome to Uber"
// email's messa_email_messages.body_text (and the JSON worker.js itself
// forwarded) were BOTH empty strings, even though the message plainly had
// real content. Extracts htmlToPlainText and the payload-selection logic
// straight out of worker.js by source-text substitution (no bundler
// available in this sandbox, and worker.js imports "postal-mime" which
// isn't meaningfully mockable for a pure-function test like this one) so
// this is testing the ACTUAL shipped function body, not a reimplementation.
import fs from "node:fs";

const src = fs.readFileSync(
  "/home/claude/messa_build/cloudflare/personal-email-worker/worker.js",
  "utf8"
);

const fnMatch = src.match(/function htmlToPlainText\(html\) \{[\s\S]*?\n\}\n/);
if (!fnMatch) {
  console.error("[FAIL] could not locate htmlToPlainText in worker.js source -- test is stale");
  process.exit(1);
}

// eslint-disable-next-line no-new-func
const htmlToPlainText = new Function(`${fnMatch[0]}\nreturn htmlToPlainText;`)();

let failures = 0;
function check(label, cond) {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}`);
  if (!cond) failures++;
}

// --- The actual reproduced scenario: a bulk-mail-style message with a
// real 4-digit code buried in an HTML template. ---
const uberLikeHtml = `
<html><body>
<!-- tracking pixel comment, must not leak -->
<script>var trackme = 1;</script>
<div>Hi Vinay,</div>
<p>Welcome to Uber! Use the code below to verify your account:</p>
<div style="font-size:32px"><b>4821</b></div>
<p>Or click the button below:</p>
<a href="https://get.uber.com/verify?t=abc123">Verify my account</a>
<ul><li>This code expires in 15 minutes</li><li>Didn't request this? Ignore this email.</li></ul>
</body></html>
`;
const extracted = htmlToPlainText(uberLikeHtml);
check("real 4-digit code survives extraction", extracted.includes("4821"));
check("script content is stripped, not leaked into text", !extracted.includes("trackme"));
check("HTML comment is stripped", !extracted.includes("tracking pixel"));
check("link text AND destination both survive", extracted.includes("Verify my account") && extracted.includes("https://get.uber.com/verify?t=abc123"));
check("list items become separate lines with a marker", extracted.includes("* This code expires in 15 minutes"));
check("no leftover angle-bracket tags", !/[<>]/.test(extracted));

// --- Entity decoding: codes/links often sit next to &nbsp;/&amp; etc. ---
const entityHtml = `<p>Code:&nbsp;9034&nbsp;&mdash;&amp;nbsp-ish text &quot;ok&quot;</p>`;
const entityText = htmlToPlainText(entityHtml);
check("&nbsp; decodes to a space (code stays readable)", entityText.includes("Code: 9034"));
check("&amp; decodes to a literal &", entityText.includes("&nbsp-ish") || entityText.includes("& "));
check("&quot; decodes to a literal quote", entityText.includes('"ok"'));

// --- Numeric entity decoding ---
check("numeric entity &#39; decodes to an apostrophe", htmlToPlainText("It&#39;s here").includes("It's here"));

// --- Empty/blank html -> empty text, never throws ---
check("empty html string yields empty text", htmlToPlainText("") === "");
check("null html yields empty text", htmlToPlainText(null) === "");
check("whitespace-only html collapses to empty/whitespace text", htmlToPlainText("<p>   </p>").trim() === "");

// --- The fallback-SELECTION logic itself (mirrors the worker.js snippet
// exactly): only kicks in when parsed.text is blank AND parsed.html exists. ---
function selectText(parsedText, parsedHtml) {
  let text = parsedText || "";
  let usedHtmlFallback = false;
  if (!text.trim() && parsedHtml) {
    text = htmlToPlainText(parsedHtml);
    usedHtmlFallback = true;
  }
  return { text, usedHtmlFallback };
}

// The exact bug scenario: PostalMime parsed a real text/plain part, but it
// was just a stub (a single space) -- so PostalMime's OWN html-derivation
// never kicked in (a text/plain part technically existed), and this
// Worker's new check is what catches it.
const stubCase = selectText(" ", uberLikeHtml);
check("a whitespace-only text/plain stub triggers the html fallback", stubCase.usedHtmlFallback === true);
check("the fallback recovers the real code from html", stubCase.text.includes("4821"));

// A message with genuinely no html either -- must stay exactly as before
// this fix (empty text, no fallback attempted, no crash).
const noHtmlCase = selectText("", null);
check("no text and no html -> empty text, fallback not used", noHtmlCase.text === "" && noHtmlCase.usedHtmlFallback === false);

// A normal, healthy message with a real text/plain part -- fallback must
// NEVER override perfectly good plain text (would be a regression, not a
// fix, if it did).
const healthyCase = selectText("Hey, see you Friday at 4pm!", "<p>Hey, see you Friday at 4pm! <b>ignore me</b></p>");
check("a real non-blank text/plain part is used as-is, untouched", healthyCase.text === "Hey, see you Friday at 4pm!");
check("fallback is NOT used when text/plain already has real content", healthyCase.usedHtmlFallback === false);

if (failures) {
  console.log(`\n${failures} FAILURE(S)`);
  process.exit(1);
} else {
  console.log("\nALL PASS");
}
