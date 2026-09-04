// Cloudflare Email Worker: the inbound half of Messa's own personal-inbox
// email (see ../../messa/tools/personal_inbox_tools.py for the full
// pipeline). A Cloudflare Email Routing catch-all rule on your domain sends
// every message addressed to anyone@<your domain> here; this Worker parses
// it and hands it to Messa's own backend as plain JSON over HTTPS.
//
// Deliberately dumb: no business logic lives here. All the "who is this
// user, have we already seen this exact email, what should Messa do about
// it" decisions happen server-side (messa/server.py's
// POST /webhooks/personal-email/inbound) -- this file's only job is
// "parse the raw MIME message Cloudflare handed us, forward it as JSON."
//
// Setup (see the README's "Messa's own inbox" section for the full walkthrough):
//   1. `npm install` in this directory.
//   2. `wrangler secret put MESSA_WEBHOOK_SECRET` -- must match
//      MESSA_PERSONAL_EMAIL_WEBHOOK_SECRET in Messa's own .env.
//   3. Set MESSA_WEBHOOK_URL in wrangler.toml to your deployed server's
//      /webhooks/personal-email/inbound URL.
//   4. `wrangler deploy`.
//   5. In the Cloudflare dashboard: Email > Email Routing > enable it for
//      your domain (this provisions the MX/TXT records for you), then add
//      a catch-all routing rule pointed at this Worker.
import PostalMime from "postal-mime";

// Hard ceiling on a single attachment this Worker will even bother
// forwarding -- separate from (and smaller than) messa/config.py's own
// MAX_PDF_READ_BYTES, which is the real limit server.py enforces
// independently. This one exists purely so a huge attachment doesn't
// inflate the JSON payload this Worker POSTs with something that's going
// to get rejected server-side anyway. Compared against the base64 STRING
// length (roughly 4/3 of the raw byte count) since attachmentEncoding:
// "base64" below means that's the form attachment.content already arrives
// in -- no manual ArrayBuffer->base64 conversion needed.
const MAX_FORWARDED_ATTACHMENT_BASE64_CHARS = 20 * 1024 * 1024;

// Fallback HTML->text conversion, used ONLY when PostalMime's own `.text`
// comes back empty/whitespace-only despite the message having an `.html`
// part. This is NOT working around a general gap in PostalMime -- its own
// parser (src/text-format.js) already derives `.text` from `.html`
// whenever a message has no text/plain part at all, and that path works
// fine for most HTML-only mail.
//
// The real, confirmed-by-evidence gap is narrower: a message that DOES
// carry a text/plain alternative, but where that part is a near-empty
// "stub" (a single space, a placeholder line, or literally nothing) --
// a common pattern from bulk/transactional senders who include it purely
// for spam-score hygiene, expecting every real client to render the html
// part instead. Because a text/plain part technically exists, PostalMime
// has no reason to fall back to its own html-derived text, and `.text`
// ends up empty even though `.html` has the entire real message (this is
// exactly what happened to the actual "Welcome to Uber" email that
// triggered this fix: messa_email_messages.body_text and the JSON this
// Worker forwarded were BOTH confirmed empty strings, even though the
// message plainly had visible content). So: whenever OUR OWN check of
// parsed.text finds it blank, we do the same kind of conversion
// PostalMime would have done for us, using whatever's in parsed.html --
// regardless of which branch inside PostalMime produced the empty
// result. Deliberately simple/regex-based (this Worker has no DOM), same
// spirit as PostalMime's own text-format.js: strip script/style content
// first (never want their text leaking in), turn common block-level tags
// into line breaks so paragraphs/list items don't run together, keep link
// destinations visible (a bare "click here" is useless once the <a> tag
// is gone), decode the handful of HTML entities actually likely to appear
// in a verification code/OTP email, then strip whatever tags remain.
function htmlToPlainText(html) {
  if (!html) return "";
  let text = html;
  text = text.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  text = text.replace(/<!--[\s\S]*?-->/g, " ");
  text = text.replace(/<a\b[^>]*href=["']([^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi, (_, href, inner) => {
    const label = inner.replace(/<[^>]+>/g, " ").trim();
    return href && href !== label ? `${label} (${href})` : label;
  });
  text = text.replace(/<li[^>]*>/gi, "\n* ");
  text = text.replace(/<\/(p|div|tr|table|h[1-6])>/gi, "\n");
  text = text.replace(/<br\s*\/?>/gi, "\n");
  text = text.replace(/<[^>]+>/g, "");
  const entities = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
  };
  text = text.replace(/&nbsp;|&amp;|&lt;|&gt;|&quot;|&#39;|&apos;/g, (m) => entities[m]);
  text = text.replace(/&#(\d+);/g, (_, code) => String.fromCharCode(parseInt(code, 10)));
  // Collapse repeated blank lines/trailing spaces left behind by the tag
  // stripping above, but keep real paragraph breaks (a 4-digit code alone
  // on its own line is exactly the shape we most need to preserve).
  text = text.split("\n").map((line) => line.replace(/[ \t]+/g, " ").trim()).join("\n");
  text = text.replace(/\n{3,}/g, "\n\n").trim();
  return text;
}

export default {
  async email(message, env, ctx) {
    let parsed;
    try {
      // attachmentEncoding: "base64" -- PostalMime hands back each
      // attachment's `content` as a ready-to-forward base64 string
      // instead of the default ArrayBuffer, since that's exactly the
      // shape the JSON payload below needs anyway.
      parsed = await PostalMime.parse(message.raw, { attachmentEncoding: "base64" });
    } catch (err) {
      // A message we can't even parse isn't something Messa can act on --
      // reject rather than forwarding garbage (Cloudflare returns this as
      // a bounce to the original sender, same as a nonexistent mailbox
      // would).
      console.error("postal-mime failed to parse inbound message:", err);
      message.setReject("Could not parse this message.");
      return;
    }

    const references = Array.isArray(parsed.references)
      ? parsed.references.join(" ")
      : parsed.references || null;

    // RFC 3834 loop safety (messa/db.py's messa_email_messages.auto_submitted,
    // migrations/030_email_loop_safety.sql): PostalMime already parses every
    // header into this array (keys lowercased) -- forward the raw value, if
    // present, so the backend can tell an auto-generated message (an "out of
    // office" bot, or another user's own Messa mailbox auto-replying) from a
    // real human, and refuse to auto-reply back to it.
    const autoSubmittedHeader =
      (parsed.headers || []).find((h) => h.key === "auto-submitted")?.value || null;

    // Forward only PDF-shaped attachments (by mimeType or filename
    // extension) and only ones under the size ceiling above -- messa/
    // server.py's personal_email_inbound_webhook (see messa/pdf_reader.py)
    // extracts text from whichever one arrives first; everything else
    // (photos, other file types, oversized PDFs) is just dropped here,
    // same as it always was before this existed.
    const pdfAttachments = (parsed.attachments || [])
      .filter((a) => {
        const name = (a.filename || "").toLowerCase();
        const type = (a.mimeType || "").toLowerCase();
        return type === "application/pdf" || name.endsWith(".pdf");
      })
      .filter((a) => typeof a.content === "string" && a.content.length <= MAX_FORWARDED_ATTACHMENT_BASE64_CHARS)
      .map((a) => ({
        filename: a.filename || "attachment.pdf",
        content_base64: a.content,
      }));

    // See htmlToPlainText's own comment above: PostalMime's `.text` can
    // come back blank even for a message that plainly has real content,
    // when a near-empty text/plain "stub" part sits alongside a real html
    // part. Whenever that's what happened here, fall back to converting
    // parsed.html ourselves rather than forwarding the stub as-is.
    let text = parsed.text || "";
    let usedHtmlFallback = false;
    if (!text.trim() && parsed.html) {
      text = htmlToPlainText(parsed.html);
      usedHtmlFallback = true;
    }
    if (usedHtmlFallback) {
      console.log(
        "postal-mime's .text was empty/whitespace-only; used the html->text fallback instead",
        { fallbackTextLength: text.length }
      );
    }

    const payload = {
      to: message.to,
      from: message.from,
      subject: parsed.subject || "",
      text,
      html: parsed.html || null,
      message_id: parsed.messageId || null,
      in_reply_to: parsed.inReplyTo || null,
      references,
      auto_submitted_header: autoSubmittedHeader,
      pdf_attachments: pdfAttachments,
    };

    let response;
    try {
      response = await fetch(env.MESSA_WEBHOOK_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Messa-Webhook-Secret": env.MESSA_WEBHOOK_SECRET || "",
        },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      // Network/DNS hiccup reaching Messa's own backend. Cloudflare Email
      // Workers have no built-in retry for the email() handler, and
      // rejecting here would bounce a perfectly good email back to its
      // sender over what's really our own infrastructure being briefly
      // down -- swallow it instead. Worst case this one email never
      // reaches Messa; that's a better failure mode than the sender
      // getting a confusing bounce for an address that does exist.
      console.error("Failed to reach Messa's webhook:", err);
      return;
    }

    if (!response.ok) {
      console.error(
        "Messa's webhook rejected this message:",
        response.status,
        await response.text().catch(() => "")
      );
    }
  },
};
