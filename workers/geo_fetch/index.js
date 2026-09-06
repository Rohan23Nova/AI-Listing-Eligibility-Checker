/**
 * Cloudflare Worker — geo_fetch
 *
 * This is the Tier-2 geo trip-wire component of the AI Listing Eligibility Checker.
 *
 * Tiered design rationale:
 *   This Worker is only invoked when Tier-1 (local UA-spoof fetch) detects an
 *   anomaly. It runs from Cloudflare's global edge, giving us a genuine geographic
 *   perspective on access. Two-to-three Workers (US, EU, APAC) let us distinguish
 *   global blocking from regional geo-fencing — a real pattern used by some
 *   large publishers.
 *
 * Deploy to Cloudflare free tier:
 *   wrangler publish  (from this directory)
 *   Paste the deployment URL into .env as GEO_WORKER_URL_US / EU / APAC
 *
 * Request format (POST JSON):
 *   { "url": "https://target.com", "user_agent": "GPTBot" }
 *
 * Response format:
 *   {
 *     "status": 200,
 *     "region": "us-east",
 *     "blocked": false,
 *     "redirect_url": null,
 *     "error": null
 *   }
 */

export default {
  async fetch(request, env, ctx) {
    // CORS for local development
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }

    if (request.method !== "POST") {
      return new Response(JSON.stringify({ error: "POST only" }), {
        status: 405,
        headers: { "Content-Type": "application/json" },
      });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    const { url, user_agent } = body;

    if (!url || !user_agent) {
      return new Response(
        JSON.stringify({ error: "Missing required fields: url, user_agent" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    // Validate URL to prevent SSRF
    let parsedUrl;
    try {
      parsedUrl = new URL(url);
    } catch {
      return new Response(JSON.stringify({ error: "Invalid URL" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      return new Response(
        JSON.stringify({ error: "Only HTTP/HTTPS URLs allowed" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }

    // Perform the fetch from this Cloudflare datacenter
    let fetchResult;
    try {
      const response = await fetch(url, {
        method: "GET",
        headers: {
          "User-Agent": user_agent,
          Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.5",
        },
        redirect: "follow",
        // Cloudflare Workers have a 30s subrequest timeout by default
      });

      const blocked =
        response.status === 403 ||
        response.status === 429 ||
        response.status === 503;

      // Get the final URL after redirects
      const finalUrl = response.url !== url ? response.url : null;

      fetchResult = {
        status: response.status,
        region: request.cf?.colo ?? "unknown",
        country: request.cf?.country ?? "unknown",
        blocked: blocked,
        redirect_url: finalUrl,
        error: null,
      };
    } catch (err) {
      fetchResult = {
        status: null,
        region: request.cf?.colo ?? "unknown",
        country: request.cf?.country ?? "unknown",
        blocked: false,
        redirect_url: null,
        error: err.message ?? "Fetch failed",
      };
    }

    return new Response(JSON.stringify(fetchResult), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
