/**
 * Cloudflare Worker: proxies apex textmessa.com and www.textmessa.com
 * to Messa's Hugging Face deployment while preserving live.textmessa.com for direct live views.
 */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Target backend: live.textmessa.com
    const targetUrl = new URL(request.url);
    targetUrl.hostname = "live.textmessa.com";
    targetUrl.protocol = "https:";
    targetUrl.port = "";

    const headers = new Headers(request.headers);
    headers.set("Host", "live.textmessa.com");
    headers.set("X-Forwarded-Host", url.hostname);
    headers.set("X-Forwarded-Proto", url.protocol.replace(":", ""));

    const init = {
      method: request.method,
      headers: headers,
      redirect: "follow",
    };

    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    const response = await fetch(targetUrl.toString(), init);
    return response;
  },
};
