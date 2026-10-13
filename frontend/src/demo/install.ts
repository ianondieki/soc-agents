import { DemoServer, type Recording } from "./server";

/**
 * The demo build's boot step (main.tsx calls it before the first render, only in demo mode):
 * load the recording, then answer every `/api/…` and `/health` request and the `/ws/ops` stream
 * from it, so the console runs with no server at all. Requests for anything else (the recording
 * itself, fonts, page chunks) go to the network as usual.
 */

let server: DemoServer | null = null;

export function demoServer(): DemoServer | null {
  return server;
}

const JSON_HEADERS = { "content-type": "application/json" };

function pathOf(input: RequestInfo | URL): URL | null {
  try {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    return new URL(raw, window.location.href);
  } catch {
    return null;
  }
}

/** The demo talks to no API anywhere, so any /api/… or /health request is the console's own (a
 *  sandboxed host frame may report an opaque origin, so the origin is not compared). */
function isApi(url: URL): boolean {
  return url.pathname.startsWith("/api/") || url.pathname === "/health";
}

async function bodyOf(input: RequestInfo | URL, init?: RequestInit): Promise<unknown> {
  let text: unknown = init?.body;
  if (text == null && input instanceof Request) text = await input.clone().text();
  if (typeof text !== "string" || !text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function respond(status: number, body: unknown): Response {
  const empty = status === 204 || body === undefined;
  return new Response(empty ? null : JSON.stringify(body), {
    status,
    // The console reads the server's clock from this header (realtime/apiHealth.ts).
    headers: { ...JSON_HEADERS, date: new Date().toUTCString() },
  });
}

/** A WebSocket that never leaves the page: `/ws/ops` frames come from the demo server. */
function fakeSocketClass(real: typeof WebSocket): typeof WebSocket {
  class DemoSocket extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSING = 2;
    readonly CLOSED = 3;
    readyState = 0;
    url: string;
    protocol = "";
    extensions = "";
    bufferedAmount = 0;
    binaryType: BinaryType = "blob";
    onopen: ((ev: Event) => void) | null = null;
    onmessage: ((ev: MessageEvent) => void) | null = null;
    onclose: ((ev: CloseEvent) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    private off: (() => void) | null = null;

    constructor(url: string | URL) {
      super();
      this.url = String(url);
      window.setTimeout(() => {
        if (this.readyState !== 0) return;
        this.readyState = 1;
        this.off = server?.connect((data) => {
          if (this.readyState !== 1) return;
          const ev = new MessageEvent("message", { data });
          this.onmessage?.(ev);
          this.dispatchEvent(ev);
        }) ?? null;
        const ev = new Event("open");
        this.onopen?.(ev);
        this.dispatchEvent(ev);
      }, 30);
    }

    send(): void {
      /* the ops stream takes nothing from the page */
    }

    close(): void {
      if (this.readyState >= 2) return;
      this.readyState = 3;
      this.off?.();
      const ev = new CloseEvent("close", { code: 1000, wasClean: true });
      this.onclose?.(ev);
      this.dispatchEvent(ev);
    }
  }
  return new Proxy(real, {
    construct(target, args: [string | URL, (string | string[])?]) {
      const url = String(args[0]);
      if (/\/ws\/ops(\?|$)/.test(url)) return new DemoSocket(url) as unknown as WebSocket;
      return new target(...args);
    },
  });
}

/** Load the recording and answer the API from it. Resolves once the console can render. */
export async function installDemo(): Promise<void> {
  const realFetch = window.fetch.bind(window);
  const res = await realFetch(new URL("demo-data/recording.json", document.baseURI).href);
  if (!res.ok) throw new Error(`The demo recording did not load (${res.status}).`);
  const rec = (await res.json()) as Recording;
  server = new DemoServer(rec);

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = pathOf(input);
    if (!url || !isApi(url) || !server) return realFetch(input as RequestInfo, init);
    const method = (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const body = method === "GET" || method === "HEAD" ? null : await bodyOf(input, init);
    if (url.pathname === "/health") return respond(200, { status: "ok", operator: "safaricom" });
    // A tick of latency, so a page's "Loading…" state is the one it shows on a real network.
    await new Promise((r) => window.setTimeout(r, 40 + Math.random() * 80));
    const answer = await server.answer(method, url.pathname, url.searchParams, body);
    return respond(answer.s, answer.b);
  };
  window.WebSocket = fakeSocketClass(window.WebSocket);
}

/** Start over: before the storm, nothing pressed, the guide at its first step. */
export function resetDemo(): void {
  server?.reset();
  try {
    for (const k of Object.keys(sessionStorage)) if (k.startsWith("noc_")) sessionStorage.removeItem(k);
  } catch {
    /* nothing stored */
  }
  window.location.hash = "#/";
  window.location.reload();
}
