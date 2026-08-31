/**
 * Typed fetch wrapper for the Reclaim /api/v1/* JSON namespace.
 *
 * Every response is decoded from JSON and normalized into one of:
 *   - a typed value on 2xx
 *   - an ApiError (carrying the HTTP status + a machine detail) otherwise
 *
 * The Vite dev server proxies /api to the FastAPI backend (see vite.config.ts),
 * so the fetch base is a relative path and no CORS is involved in dev. For a
 * real deployment, point VITE_API_BASE_URL at the API origin (and see
 * frontend/README.md for the CORS caveat).
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
}

async function request<T>(
  path: string,
  { method = "GET", body, signal }: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    // Network failure / abort — surface as a distinguishable error, not a 4xx.
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    throw new ApiError(0, "Network error — could not reach the Reclaim API.");
  }

  if (!res.ok) {
    let detail = `Request failed with status ${res.status}.`;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") {
        detail = data.detail;
      }
    } catch {
      // Non-JSON error body — keep the fallback message.
    }
    throw new ApiError(res.status, detail);
  }

  return (await res.json()) as T;
}

export const apiClient = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body }),
};
