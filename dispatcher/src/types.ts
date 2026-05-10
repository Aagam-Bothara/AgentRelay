// Shared types for the dispatcher Worker.

export interface Env {
  INSTALL: DurableObjectNamespace;
  STATE: KVNamespace;
  PUBLIC_URL: string;
  // Set via `wrangler secret put`:
  SLACK_CLIENT_ID: string;
  SLACK_CLIENT_SECRET: string;
  SLACK_SIGNING_SECRET: string;
}

// OAuth session — short-lived, key in KV is `oauth:<session_token>`.
export interface OAuthSession {
  state: "pending" | "complete";
  install_id?: string;
  install_secret?: string;
  bot_token?: string;
  team_id?: string;
  team_name?: string;
  user_id?: string;
  authed_user_id?: string;
  error?: string;
  created_at: number;
}

// Mapping stored in KV at key `team:<team_id>:<authed_user_id>` to find the
// DO for a given Slack user. Used to route interactivity callbacks.
export interface InstallPointer {
  install_id: string;
}

// Internal payload sent from Worker -> DO over websocket.
export interface ForwardedAction {
  type: "approval";
  approval_id: string;
  decision: "approve" | "reject";
  user_id: string; // slack user who clicked
}

// Slack OAuth v2 access response.
export interface SlackOAuthV2Response {
  ok: boolean;
  error?: string;
  access_token?: string;
  scope?: string;
  bot_user_id?: string;
  team?: { id: string; name: string };
  authed_user?: { id: string };
}
