// AgentRelay dispatcher Worker.
//
// What it does:
//   - Slack OAuth v2 endpoints (start, callback, poll).
//   - Slack interactivity webhook (button click router).
//   - Per-install websocket endpoint that the local CLI keeps open.
//
// What it does NOT do:
//   - Look at the actual command being approved. The button `value` carries
//     only an opaque approval_id and the install_id used to route. Commands
//     and project data never leave the user's machine.

import { Env, OAuthSession, ForwardedAction, SlackOAuthV2Response, InstallPointer } from "./types";
export { Install } from "./install";

// im:write lets the bot DM the installer directly so the user never has to
// pick a channel during setup. Bot DMs are how all approval messages flow.
const SCOPES = "chat:write,commands,im:write";
const OAUTH_TTL_SECONDS = 600;

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
  });
}

function html(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function randomId(bytes: number): string {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function verifySlackSignature(req: Request, body: string, signingSecret: string): Promise<boolean> {
  const ts = req.headers.get("X-Slack-Request-Timestamp");
  const sig = req.headers.get("X-Slack-Signature");
  if (!ts || !sig) return false;
  // Reject replays older than 5 min.
  if (Math.abs(Date.now() / 1000 - parseInt(ts, 10)) > 300) return false;

  const base = `v0:${ts}:${body}`;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(signingSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const macBuf = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(base));
  const macHex = Array.from(new Uint8Array(macBuf), (b) => b.toString(16).padStart(2, "0")).join("");
  const expected = `v0=${macHex}`;

  if (expected.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}

// ---------- routes ----------

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/" || url.pathname === "/healthz") {
      return json({ ok: true, service: "agentrelay-dispatcher" });
    }

    if (url.pathname === "/oauth/start") return handleOAuthStart(req, env);
    if (url.pathname === "/oauth/callback") return handleOAuthCallback(req, env);
    if (url.pathname === "/oauth/poll") return handleOAuthPoll(req, env);
    if (url.pathname === "/slack/interactive") return handleSlackInteractive(req, env);
    if (url.pathname.startsWith("/ws/")) return handleWebsocket(req, env, url.pathname.slice(4));

    return new Response("Not found", { status: 404 });
  },
};

// GET /oauth/start?session=<rand>
// CLI generates a session token, opens this URL in the user's browser. We
// remember the session, then redirect to Slack's OAuth page.
async function handleOAuthStart(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const session = url.searchParams.get("session");
  if (!session || session.length < 16) return new Response("missing/invalid session", { status: 400 });

  const initial: OAuthSession = { state: "pending", created_at: Date.now() };
  await env.STATE.put(`oauth:${session}`, JSON.stringify(initial), { expirationTtl: OAUTH_TTL_SECONDS });

  const slackAuth = new URL("https://slack.com/oauth/v2/authorize");
  slackAuth.searchParams.set("client_id", env.SLACK_CLIENT_ID);
  slackAuth.searchParams.set("scope", SCOPES);
  slackAuth.searchParams.set("redirect_uri", `${env.PUBLIC_URL}/oauth/callback`);
  slackAuth.searchParams.set("state", session);
  return Response.redirect(slackAuth.toString(), 302);
}

// GET /oauth/callback?code=...&state=<session>
// Slack hands us back the code; we exchange it for a bot token, allocate an
// install_id + install_secret, store everything, and tell the user "done —
// you can close this tab".
async function handleOAuthCallback(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const session = url.searchParams.get("state");
  if (!code || !session) return html("Missing code or state.", 400);

  const sessionRaw = await env.STATE.get(`oauth:${session}`);
  if (!sessionRaw) return html("Session expired or unknown. Re-run <code>agentrelay login</code>.", 400);

  const tokenResp = await fetch("https://slack.com/api/oauth.v2.access", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: env.SLACK_CLIENT_ID,
      client_secret: env.SLACK_CLIENT_SECRET,
      code,
      redirect_uri: `${env.PUBLIC_URL}/oauth/callback`,
    }),
  });
  const tok: SlackOAuthV2Response = await tokenResp.json();
  if (!tok.ok || !tok.access_token || !tok.team || !tok.authed_user) {
    const errSession: OAuthSession = { state: "complete", error: tok.error || "slack_oauth_failed", created_at: Date.now() };
    await env.STATE.put(`oauth:${session}`, JSON.stringify(errSession), { expirationTtl: OAUTH_TTL_SECONDS });
    return html(`Slack OAuth failed: ${tok.error || "unknown"}. You can close this tab.`, 400);
  }

  const install_id = randomId(16);
  const install_secret = randomId(32);

  // Bind install_id to the DO and store the secret + bot token inside the DO.
  const stub = env.INSTALL.get(env.INSTALL.idFromName(install_id));
  await stub.fetch("https://do/init", {
    method: "POST",
    body: JSON.stringify({
      install_secret,
      bot_token: tok.access_token,
      team_id: tok.team.id,
      team_name: tok.team.name,
      authed_user_id: tok.authed_user.id,
    }),
  });

  // KV pointer team_id+user -> install_id, used by the interactivity router
  // when it doesn't know the install_id (e.g. slash command without explicit value).
  const pointer: InstallPointer = { install_id };
  await env.STATE.put(`team:${tok.team.id}:${tok.authed_user.id}`, JSON.stringify(pointer));

  const done: OAuthSession = {
    state: "complete",
    install_id,
    install_secret,
    bot_token: tok.access_token,
    team_id: tok.team.id,
    team_name: tok.team.name,
    authed_user_id: tok.authed_user.id,
    created_at: Date.now(),
  };
  await env.STATE.put(`oauth:${session}`, JSON.stringify(done), { expirationTtl: OAUTH_TTL_SECONDS });

  return html(`
<!doctype html>
<html><head><title>AgentRelay — connected</title>
<style>body{font:14px -apple-system,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;}h1{font-size:18px}code{background:#eee;padding:2px 4px;border-radius:3px}</style>
</head><body>
<h1>✅ AgentRelay is connected to <em>${tok.team.name}</em></h1>
<p>You can close this tab. Your terminal will pick up the connection automatically.</p>
</body></html>`);
}

// GET /oauth/poll?session=<session>
// Local CLI polls until OAuth completes, then pulls the credentials.
async function handleOAuthPoll(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const session = url.searchParams.get("session");
  if (!session) return json({ error: "missing session" }, { status: 400 });

  const raw = await env.STATE.get(`oauth:${session}`);
  if (!raw) return json({ state: "expired" });
  const sess: OAuthSession = JSON.parse(raw);
  if (sess.state === "pending") return json({ state: "pending" });

  if (sess.error) return json({ state: "error", error: sess.error });

  // Single-use: delete after successful pull.
  await env.STATE.delete(`oauth:${session}`);
  return json({
    state: "complete",
    install_id: sess.install_id,
    install_secret: sess.install_secret,
    bot_token: sess.bot_token,
    team_id: sess.team_id,
    team_name: sess.team_name,
    slack_user_id: sess.authed_user_id,
  });
}

// POST /slack/interactive
// Slack delivers button clicks here as application/x-www-form-urlencoded with
// a `payload` field containing the JSON. We extract the install_id from the
// button value, look up its DO, and forward.
async function handleSlackInteractive(req: Request, env: Env): Promise<Response> {
  const body = await req.text();
  if (!(await verifySlackSignature(req, body, env.SLACK_SIGNING_SECRET))) {
    return new Response("invalid signature", { status: 401 });
  }

  const params = new URLSearchParams(body);
  const payloadRaw = params.get("payload");
  if (!payloadRaw) return new Response("missing payload", { status: 400 });

  let payload: any;
  try {
    payload = JSON.parse(payloadRaw);
  } catch {
    return new Response("malformed payload", { status: 400 });
  }
  if (payload.type !== "block_actions") return json({ ok: true });

  for (const action of payload.actions || []) {
    // Button value format: `<install_id>:<approval_id>:<decision>`
    const value: string = action.value || "";
    const parts = value.split(":");
    if (parts.length !== 3) continue;
    const [install_id, approval_id, decision] = parts;
    if (decision !== "approve" && decision !== "reject") continue;

    const stub = env.INSTALL.get(env.INSTALL.idFromName(install_id));
    const fwd: ForwardedAction = {
      type: "approval",
      approval_id,
      decision,
      user_id: payload.user?.id || "",
    };
    // Fire-and-forget; DO internally writes to its websocket if connected.
    await stub.fetch("https://do/forward", {
      method: "POST",
      body: JSON.stringify(fwd),
    });
  }

  return json({ ok: true });
}

// GET /ws/<install_id>  (Upgrade: websocket)
// Local CLI opens this and keeps it open. Authenticated by the install_secret
// passed in the `Authorization: Bearer <secret>` header. The DO verifies.
async function handleWebsocket(req: Request, env: Env, install_id: string): Promise<Response> {
  if (req.headers.get("Upgrade") !== "websocket") {
    return new Response("expected websocket upgrade", { status: 426 });
  }
  if (!install_id) return new Response("missing install_id", { status: 400 });
  const stub = env.INSTALL.get(env.INSTALL.idFromName(install_id));
  return stub.fetch(req);
}
