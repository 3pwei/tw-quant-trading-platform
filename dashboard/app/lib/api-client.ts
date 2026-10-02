const configuredApiBase = process.env.NEXT_PUBLIC_MARKET_API_URL?.replace(
  /\/$/,
  "",
);

export function apiBase(): string {
  return configuredApiBase
    || (typeof window === "undefined" ? "http://localhost:8000" : window.location.origin);
}

export function apiUrl(path: string): string {
  return `${apiBase()}${path.startsWith("/") ? path : `/${path}`}`;
}

export async function responseBody<T>(
  response: Response,
  fallbackMessage: string,
): Promise<T> {
  const body = await response.json() as T & { detail?: string };
  if (!response.ok) {
    throw new Error(body.detail ?? `${fallbackMessage} (${response.status})`);
  }
  return body;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  fallbackMessage = "API 回應異常",
): Promise<T> {
  return responseBody<T>(await fetch(apiUrl(path), init), fallbackMessage);
}

export function jsonRequest(method: "POST" | "PUT" | "DELETE", body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
