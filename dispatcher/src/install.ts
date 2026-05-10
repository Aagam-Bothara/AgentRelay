// Install Durable Object — one per AgentRelay install.
//
// Holds:
//   - install_secret  (used to authenticate the websocket connection)
//   - bot_token       (returned to the local CLI on first poll; not used here otherwise)
//   - team metadata   (for diagnostics)
//   - the active websocket from the local CLI (if connected)
//
// Receives:
//   - POST /init     (one-time during OAuth callback; stores secret + metadata)
//   - POST /forward  (interactivity router calls this; we relay to websocket)
//   - GET  /         (websocket upgrade from local CLI)

import { ForwardedAction } from "./types";

interface StoredState {
  install_secret: string;
  bot_token: string;
  team_id: string;
  team_name: string;
  authed_user_id: string;
  initialized_at: number;
}

export class Install implements DurableObject {
  private state: DurableObjectState;
  private socket: WebSocket | null = null;
  private pending: ForwardedAction[] = [];

  constructor(state: DurableObjectState) {
    this.state = state;
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/init") return this.handleInit(req);
    if (url.pathname === "/forward") return this.handleForward(req);
    if (req.headers.get("Upgrade") === "websocket") return this.handleWebsocket(req);

    return new Response("Not found", { status: 404 });
  }

  private async handleInit(req: Request): Promise<Response> {
    const body = (await req.json()) as Omit<StoredState, "initialized_at">;
    const stored: StoredState = { ...body, initialized_at: Date.now() };
    await this.state.storage.put("state", stored);
    return new Response("ok");
  }

  private async handleForward(req: Request): Promise<Response> {
    const action = (await req.json()) as ForwardedAction;
    if (this.socket && this.socket.readyState === WebSocket.READY_STATE_OPEN) {
      try {
        this.socket.send(JSON.stringify(action));
      } catch {
        this.pending.push(action);
      }
    } else {
      // Buffer until the CLI reconnects; cap at 50 to bound memory.
      this.pending.push(action);
      if (this.pending.length > 50) this.pending.shift();
    }
    return new Response("ok");
  }

  private async handleWebsocket(req: Request): Promise<Response> {
    const stored = (await this.state.storage.get<StoredState>("state")) || null;
    if (!stored) return new Response("install not initialized", { status: 404 });

    const auth = req.headers.get("Authorization") || "";
    const expected = `Bearer ${stored.install_secret}`;
    if (auth !== expected) return new Response("unauthorized", { status: 401 });

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    server.accept();
    this.socket = server;

    // Drain any actions that arrived while we were disconnected.
    for (const buffered of this.pending) {
      try {
        server.send(JSON.stringify(buffered));
      } catch {
        // ignore
      }
    }
    this.pending = [];

    server.addEventListener("close", () => {
      if (this.socket === server) this.socket = null;
    });
    server.addEventListener("error", () => {
      if (this.socket === server) this.socket = null;
    });

    return new Response(null, { status: 101, webSocket: client });
  }
}
