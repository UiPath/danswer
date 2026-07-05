// Open-redirect-safe validation for the post-login "next" return path.
//
// After SSO, we want to send the user back to the deep link they clicked
// (e.g. /chat?assistant=AutomationSuite). The `next` value is attacker-
// controllable (it rides in a URL / cookie), so it must be validated on the
// TRUSTED side before we ever issue a redirect to it. We accept ONLY same-origin
// ROOT-RELATIVE paths under an explicit allowlist of app entry points — never an
// absolute URL, protocol-relative (`//host`), backslash-escaped (`/\host`),
// userinfo-smuggled (`https://trusted@evil`), or scheme (`javascript:`) value.
//
// Validation uses a real URL parser (not string prefix/contains checks, which the
// oauth-oidc guidance calls out as bypassable), and is applied at BOTH the
// set-cookie and consume-redirect boundaries (defense in depth).

// Cookie that carries the validated post-login return path across the SSO
// round-trip (set on /auth/login by middleware, consumed by the auth callbacks).
export const LOGIN_NEXT_COOKIE = "darwin_login_next";

// App pages a post-login redirect may land on. Deliberately excludes /admin,
// /auth, etc. — a login flow should never bounce a user into a privileged route.
const ALLOWED_NEXT_PREFIXES = ["/chat"];

const MAX_NEXT_LENGTH = 512;

// A dummy same-origin base; if a parsed value's origin differs from this, the
// input smuggled in an absolute URL and must be rejected.
const SENTINEL_ORIGIN = "https://darwin.invalid";

// Control chars (incl. CR/LF, which enable header/cookie injection).
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/;

/**
 * Returns a safe, same-origin, root-relative path (pathname + query) to redirect
 * to after login, or `null` if the input is missing or unsafe. Callers must treat
 * `null` as "fall back to the default landing page".
 */
export function getSafeNextPath(next: string | null | undefined): string | null {
  if (!next || typeof next !== "string") return null;
  if (next.length > MAX_NEXT_LENGTH) return null;
  if (CONTROL_CHARS.test(next)) return null;

  // Must be root-relative. Reject protocol-relative (`//`) and backslash variants
  // (`/\`, and their encoded forms) that browsers can treat as a host.
  if (!next.startsWith("/")) return null;
  const head = next.slice(0, 4).toLowerCase();
  if (
    next.startsWith("//") ||
    next.startsWith("/\\") ||
    head.startsWith("/%2f") ||
    head.startsWith("/%5c")
  ) {
    return null;
  }

  let parsed: URL;
  try {
    parsed = new URL(next, SENTINEL_ORIGIN);
  } catch {
    return null;
  }

  // If parsing changed the origin, an absolute URL was smuggled in.
  if (parsed.origin !== SENTINEL_ORIGIN) return null;

  // Destination page must be explicitly allowlisted.
  const path = parsed.pathname;
  const allowed = ALLOWED_NEXT_PREFIXES.some(
    (p) => path === p || path.startsWith(p + "/")
  );
  if (!allowed) return null;

  // Return only path + query — never any host or fragment.
  return parsed.pathname + parsed.search;
}
