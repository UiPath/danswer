import { getDomain } from "@/lib/redirectSS";
import { getSafeNextPath, LOGIN_NEXT_COOKIE } from "@/lib/safeRedirect";
import { buildUrl } from "@/lib/utilsSS";
import { NextRequest, NextResponse } from "next/server";

// have to use this so we don't hit the redirect URL with a `POST` request
const SEE_OTHER_REDIRECT_STATUS = 303;

export const POST = async (request: NextRequest) => {
  // Wrapper around the FastAPI endpoint /auth/saml/callback,
  // which adds back a redirect to the main app.
  const url = new URL(buildUrl("/auth/saml/callback"));
  url.search = request.nextUrl.search;

  const response = await fetch(url.toString(), {
    method: "POST",
    body: await request.formData(),
  });
  const setCookieHeader = response.headers.get("set-cookie");

  if (!setCookieHeader) {
    return NextResponse.redirect(
      new URL("/auth/error", getDomain(request)),
      SEE_OTHER_REDIRECT_STATUS
    );
  }

  // Return the user to the deep link they started from (stashed on /auth/login),
  // re-validated open-redirect-safe. Falls back to the app home if absent/unsafe.
  const nextPath =
    getSafeNextPath(request.cookies.get(LOGIN_NEXT_COOKIE)?.value) ?? "/";

  const redirectResponse = NextResponse.redirect(
    new URL(nextPath, getDomain(request)),
    SEE_OTHER_REDIRECT_STATUS
  );
  redirectResponse.headers.set("set-cookie", setCookieHeader);
  // Clear the one-shot next cookie (append so the session Set-Cookie above stays).
  redirectResponse.headers.append(
    "set-cookie",
    `${LOGIN_NEXT_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax`
  );
  return redirectResponse;
};
