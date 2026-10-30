export class ApiError extends Error {
  constructor(public status: number, message: string, public data?: unknown) {
    super(message);
  }
}

function csrf(): string {
  // "__Host-acc_csrf" over HTTPS (it can't be planted by a sibling site), "acc_csrf" on plain-HTTP test servers.
  const name = window.location.protocol === "https:" ? "__Host-acc_csrf=" : "acc_csrf=";
  const match = document.cookie.split(";").map(c => c.trim()).find(c => c.startsWith(name));
  return match ? match.slice(name.length) : "";
}

/** Only an explicit pre-handler CSRF refusal is replayable; never retry an authorization error. */
async function csrfFetch(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has("X-ACC-CSRF")) headers.set("X-ACC-CSRF", csrf());
  const response = await fetch(path, { ...init, credentials: "same-origin", headers });
  if (response.status !== 403 || response.headers.get("X-ACC-CSRF-Error") !== "mismatch") return response;
  try {
    const fresh = await fetch("/api/auth/csrf", { credentials: "same-origin", cache: "no-store" });
    if (!fresh.ok) return response;
    const body = await fresh.json();
    if (typeof body.csrf !== "string" || !body.csrf) return response;
    headers.set("X-ACC-CSRF", body.csrf);
  } catch { return response; }
  // One retry, using this session's token directly. No module cache can outlive a sign-in in another tab.
  return fetch(path, { ...init, credentials: "same-origin", headers });
}

/** A form the server refused field by field (FastAPI's 422 list), as one sentence: "name: give the project a name". */
function fieldErrors(detail: unknown): string | undefined {
  if (!Array.isArray(detail) || !detail.length) return undefined;
  return detail.map((e: { loc?: unknown[]; msg?: string }) => {
    const field = (e.loc ?? []).filter((x) => x !== "body").join(" ");
    return `${field ? `${field.replace(/_/g, " ")}: ` : ""}${(e.msg ?? "not valid").replace(/^Value error, /, "")}`;
  }).join("; ");
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const response = await csrfFetch(path, {
    credentials: "same-origin",
    ...rest,
    method: rest.method ?? (json !== undefined ? "POST" : "GET"),
    headers: {
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      "X-ACC-CSRF": csrf(),
      ...headers,
    },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (!response.ok) {
    let message = response.statusText;
    let data: unknown;
    try {
      const body = await response.json();
      data = body;
      message = typeof body.detail === "string" ? body.detail : fieldErrors(body.detail) ?? JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    if (response.status === 401 && !path.startsWith("/api/auth/")) window.location.assign("/login");
    throw new ApiError(response.status, message, data);
  }
  return response.json() as Promise<T>;
}

/** The server said there is no such thing (404): a page shows "not here" with a way back, not an error. */
export const isNotFound = (error: unknown) => error instanceof ApiError && error.status === 404;

export const post = <T,>(path: string, json: unknown = {}) => api<T>(path, { json });

/** Secret-bearing exports bypass query/mutation result caches and are downloaded directly. */
export async function downloadFile(path: string, filename: string, json: unknown): Promise<void> {
  const response = await csrfFetch(path, {method:'POST', credentials:'same-origin', cache:'no-store',
    headers:{'Content-Type':'application/json','X-ACC-CSRF':csrf()}, body:JSON.stringify(json)});
  if (!response.ok) throw new ApiError(response.status, 'Export failed. Check your owner sign-in and try again.');
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Bounded Files batches expose transport progress; only a pre-handler CSRF mismatch may replay. */
export async function uploadJson<T>(path: string, json: unknown, progress: (percent: number) => void): Promise<T> {
  const body = JSON.stringify(json);
  const send = (token: string) => new Promise<XMLHttpRequest>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.timeout = 120_000;
    xhr.ontimeout = () => reject(new Error("Upload timed out. Check the folder before trying again."));
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("X-ACC-CSRF", token);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) progress(Math.round(e.loaded / e.total * 100)); };
    xhr.onload = () => resolve(xhr);
    xhr.onerror = () => reject(new Error("Upload connection lost. Check the folder before trying again."));
    xhr.onabort = () => reject(new Error("Upload interrupted. Check the folder before trying again."));
    xhr.send(body);
  });
  let response = await send(csrf());
  if (response.status === 403 && response.getResponseHeader("X-ACC-CSRF-Error") === "mismatch") {
    const fresh = await fetch("/api/auth/csrf", { credentials: "same-origin", cache: "no-store" });
    if (fresh.ok) {
      const token = await fresh.json();
      if (typeof token.csrf === "string" && token.csrf) response = await send(token.csrf);
    }
  }
  let result;
  try { result = JSON.parse(response.responseText); } catch { throw new Error("Upload response unreadable. Check the folder before trying again."); }
  if (response.status < 200 || response.status >= 300) throw new ApiError(response.status,
    typeof result.detail === "string" ? result.detail : fieldErrors(result.detail) ?? "Upload refused.");
  return result as T;
}
