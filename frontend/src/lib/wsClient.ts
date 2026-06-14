// WebSocket client helper for the DRONA live rooms.
//
// Connects to a /ws/... endpoint with the access token as a ?token= query
// param (browsers cannot set Authorization headers on a WS handshake), parses
// incoming WSMessage envelopes, dispatches them to typed listeners, and
// answers the manager's heartbeat ping (every 30s) with a pong frame.
//
// Reconnect logic intentionally lives in the dashboard hook (task 17.1); this
// helper provides a reusable connect() with onMessage/onOpen/onClose callbacks.

import type {
  TypedWSMessage,
  WSMessage,
  WSMessageType,
  WSPayloadMap,
} from "@/types";
import { tokenStore, type TokenStore } from "@/lib/tokenStore";

/** Default WS base URL when no env override is provided. */
export const DEFAULT_WS_BASE_URL = "ws://localhost:8000";

function resolveWsBaseUrl(): string {
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> })
    .env;
  const fromEnv = env?.VITE_WS_BASE_URL;
  return (fromEnv && fromEnv.length > 0 ? fromEnv : DEFAULT_WS_BASE_URL).replace(
    /\/+$/,
    "",
  );
}

/** A control frame as understood by the manager (e.g. heartbeat ping). */
interface ControlFrame {
  type?: string;
}

export type WSEventListener<K extends WSMessageType> = (
  message: TypedWSMessage<K>,
) => void;

export type WSAnyListener = (message: WSMessage) => void;

export interface WsConnectOptions {
  /** Path of the room endpoint, e.g. "/ws/dashboard". */
  path: string;
  /** Override the token (defaults to the access token from the store). */
  token?: string;
  baseUrl?: string;
  store?: TokenStore;
  /** Injectable WebSocket constructor (defaults to global WebSocket). */
  webSocketImpl?: typeof WebSocket;
  onMessage?: WSAnyListener;
  onOpen?: (event: Event) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (event: Event) => void;
}

/**
 * A live connection handle. `on`/`off` register typed listeners by message
 * type; `close` tears the socket down and stops dispatching.
 */
export interface WsConnection {
  readonly socket: WebSocket;
  on<K extends WSMessageType>(type: K, listener: WSEventListener<K>): () => void;
  off<K extends WSMessageType>(type: K, listener: WSEventListener<K>): void;
  send(data: unknown): void;
  close(code?: number, reason?: string): void;
}

function buildWsUrl(baseUrl: string, path: string, token: string | null): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const url = /^wss?:\/\//.test(path) ? path : `${baseUrl}${normalizedPath}`;
  if (!token) {
    return url;
  }
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}

function isControlFrame(value: unknown): value is ControlFrame {
  return typeof value === "object" && value !== null && "type" in value;
}

/**
 * Open a WebSocket connection to a live room and start dispatching envelopes.
 * Returns a handle for registering typed listeners and closing the socket.
 */
export function connect(options: WsConnectOptions): WsConnection {
  const baseUrl = (options.baseUrl ?? resolveWsBaseUrl()).replace(/\/+$/, "");
  const store = options.store ?? tokenStore;
  const token = options.token ?? store.getAccessToken();
  const WebSocketImpl = options.webSocketImpl ?? WebSocket;

  const url = buildWsUrl(baseUrl, options.path, token);
  const socket = new WebSocketImpl(url);

  const listeners = new Map<WSMessageType, Set<WSAnyListener>>();

  const dispatch = (message: WSMessage): void => {
    options.onMessage?.(message);
    const set = listeners.get(message.type);
    if (set) {
      for (const listener of set) {
        listener(message);
      }
    }
  };

  socket.addEventListener("message", (event: MessageEvent) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(typeof event.data === "string" ? event.data : "");
    } catch {
      return; // Ignore malformed frames.
    }

    // Answer the manager's heartbeat ping with a pong (Req 12A.3).
    if (isControlFrame(parsed) && parsed.type === "ping") {
      try {
        socket.send(JSON.stringify({ type: "pong" }));
      } catch {
        // Socket may be closing; ignore.
      }
      return;
    }

    // Only dispatch well-formed envelopes carrying a known-shaped `type`.
    if (
      isControlFrame(parsed) &&
      typeof (parsed as WSMessage).type === "string" &&
      "payload" in (parsed as Record<string, unknown>)
    ) {
      dispatch(parsed as WSMessage);
    }
  });

  if (options.onOpen) {
    socket.addEventListener("open", options.onOpen);
  }
  if (options.onClose) {
    socket.addEventListener("close", options.onClose as EventListener);
  }
  if (options.onError) {
    socket.addEventListener("error", options.onError);
  }

  return {
    socket,
    on<K extends WSMessageType>(type: K, listener: WSEventListener<K>) {
      let set = listeners.get(type);
      if (!set) {
        set = new Set();
        listeners.set(type, set);
      }
      set.add(listener as WSAnyListener);
      return () => this.off(type, listener);
    },
    off<K extends WSMessageType>(type: K, listener: WSEventListener<K>) {
      listeners.get(type)?.delete(listener as WSAnyListener);
    },
    send(data: unknown) {
      socket.send(typeof data === "string" ? data : JSON.stringify(data));
    },
    close(code?: number, reason?: string) {
      listeners.clear();
      socket.close(code, reason);
    },
  };
}

// Re-export the payload map type for consumers that build typed handlers.
export type { WSPayloadMap };
