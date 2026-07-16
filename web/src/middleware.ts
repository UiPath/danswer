import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { SERVER_SIDE_ONLY__PAID_ENTERPRISE_FEATURES_ENABLED } from "./lib/constants";
import { getSafeNextPath, LOGIN_NEXT_COOKIE } from "./lib/safeRedirect";

const eePaths = [
  "/admin/groups",
  "/admin/api-key",
  "/admin/performance/usage",
  "/admin/performance/query-history",
  "/admin/whitelabeling",
  "/admin/performance/custom-analytics",
];

export async function middleware(request: NextRequest) {
  const pathname = request.nextUrl.pathname;

  // On the login page, stash a validated post-login return path in a short-lived
  // cookie so it survives the SSO redirect to the IdP and back. Setting it here
  // (server-side) is necessary because the login page is a server component and
  // can't write cookies, and the OAuth `state` is owned by fastapi-users.
  if (pathname === "/auth/login") {
    const safeNext = getSafeNextPath(request.nextUrl.searchParams.get("next"));
    const response = NextResponse.next();
    if (safeNext) {
      response.cookies.set(LOGIN_NEXT_COOKIE, safeNext, {
        httpOnly: true,
        sameSite: "lax", // sent on the top-level GET redirect back from the IdP
        secure: true,
        path: "/",
        maxAge: 600, // 10 min — just long enough to complete a login
      });
    }
    return response;
  }

  if (SERVER_SIDE_ONLY__PAID_ENTERPRISE_FEATURES_ENABLED) {
    // Check if the current path is in the eePaths list
    if (eePaths.some((path) => pathname.startsWith(path))) {
      // Add '/ee' to the beginning of the pathname
      const newPathname = `/ee${pathname}`;

      // Create a new URL with the modified pathname
      const newUrl = new URL(newPathname, request.url);

      // Rewrite to the new URL
      return NextResponse.rewrite(newUrl);
    }
  }

  // Continue with the response if no rewrite is needed
  return NextResponse.next();
}

// Next.js statically analyses this `config` object at build time and
// rejects computed values, so the matcher list has to be inlined as
// literals. Keep this in sync with `eePaths` above — adding a path to
// one without the other will cause middleware to either skip the route
// (if missing here) or run on every route (if a non-literal sneaks back).
export const config = {
  matcher: [
    "/auth/login",
    "/admin/groups/:path*",
    "/admin/api-key/:path*",
    "/admin/performance/usage/:path*",
    "/admin/performance/query-history/:path*",
    "/admin/whitelabeling/:path*",
    "/admin/performance/custom-analytics/:path*",
  ],
};
