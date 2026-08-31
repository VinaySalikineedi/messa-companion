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

export default {
  async email(message, env, ctx) {
    let parsed;
    try {
      parsed = await PostalMime.parse(message.raw);
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

    const payload = {
      to: message.to,
      from: message.from,
      subject: parsed.subject || "",
      text: parsed.text || "",
      message_id: parsed.messageId || null,
      in_reply_to: parsed.inReplyTo || null,
      references,
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
