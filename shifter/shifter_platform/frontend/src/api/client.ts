/**
 * Single typed fetch client for the SPA. Every ADR-029 API-client convention
 * lives here, not in components:
 *  - base `/api/v1/`, single origin, `credentials: same-origin`
 *  - session cookie is the browser credential; no bearer token is sent
 *  - `X-CSRFToken` header on unsafe methods (from the csrftoken cookie)
 *  - `X-Request-ID` propagation for client/server log correlation
 *  - parse the shared error envelope into a typed `ApiError`
 * Retry/polling policy is owned by the TanStack Query client, not here.
 */
import { getCsrfToken } from "./csrf";
import { ApiError, type ApiErrorEnvelope } from "./errors";

export const API_BASE = "/api/v1";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type QueryValue = string | number | boolean | undefined | null;

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
}

export interface DownloadOptions extends RequestOptions {
  expectedMediaType: string;
  maxBytes: number;
}

function newRequestId(): string {
  // crypto.randomUUID exists only in secure contexts (HTTPS / localhost). Over
  // plain HTTP (a LAN/dev origin) it is undefined, so fall back to
  // getRandomValues, which is available in insecure contexts and is a CSPRNG
  // (not the Math.random PRNG SonarCloud S2245 flags).
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return `req-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

// Callers interpolate route params into API paths. Refuse a path that the URL
// parser would rewrite (a dot segment, plain or percent-encoded, or a
// backslash) or that carries a fragment, so a param cannot send a request to
// another endpoint (client-side path traversal).
const UNSAFE_API_PATH = /(?:^|\/)(?:\.|%2e){1,2}(?:[/?]|$)|[\\#]/i;

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  if (UNSAFE_API_PATH.test(path)) {
    throw new ApiError(400, { code: "invalid_path", message: "The request path is not valid." });
  }
  const url = new URL(`${API_BASE}${path}`, globalThis.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null) {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return `${url.pathname}${url.search}`;
}

function buildRequest(options: RequestOptions): {
  method: string;
  init: RequestInit;
} {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { "X-Request-ID": newRequestId() };

  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (UNSAFE_METHODS.has(method)) {
    headers["X-CSRFToken"] = getCsrfToken();
  }
  return {
    method,
    init: {
      method,
      headers,
      body,
      credentials: "same-origin",
      signal: options.signal,
    },
  };
}

function errorFromPayload(status: number, payload: unknown): ApiError {
  const envelope =
    (payload as { error?: ApiErrorEnvelope } | undefined)?.error ??
    ({ code: "error", message: `Request failed (${status})` } satisfies ApiErrorEnvelope);
  return new ApiError(status, envelope);
}

/**
 * Parse an error response body. A non-JSON body (for example a proxy or HTML
 * error page) has no envelope, so the caller still gets a typed `ApiError`
 * and the body text never reaches an error message.
 */
function parseErrorPayload(text: string): unknown {
  try {
    return text ? JSON.parse(text) : undefined;
  } catch {
    return undefined;
  }
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { init } = buildRequest(options);
  const response = await fetch(buildUrl(path, options.query), init);

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();

  if (!response.ok) {
    throw errorFromPayload(response.status, parseErrorPayload(text));
  }

  return (text ? JSON.parse(text) : undefined) as T;
}

/** Fetch a bounded binary response while retaining the canonical JSON error path. */
export async function apiDownload(path: string, options: DownloadOptions): Promise<Blob> {
  const { init } = buildRequest(options);
  const response = await fetch(buildUrl(path, options.query), init);
  const mediaType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase() ?? "";

  if (!response.ok || response.status === 202) {
    throw errorFromPayload(response.status, parseErrorPayload(await response.text()));
  }
  if (mediaType !== options.expectedMediaType.toLowerCase()) {
    throw new ApiError(502, { code: "unexpected_response", message: "The server returned an invalid download." });
  }

  const contentLength = Number(response.headers.get("Content-Length"));
  if (Number.isFinite(contentLength) && contentLength > options.maxBytes) {
    throw new ApiError(502, { code: "unexpected_response", message: "The download exceeded the allowed size." });
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > options.maxBytes) {
    throw new ApiError(502, { code: "unexpected_response", message: "The download exceeded the allowed size." });
  }
  return new Blob([bytes], { type: options.expectedMediaType });
}
