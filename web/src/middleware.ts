import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { SERVER_SIDE_ONLY__PAID_ENTERPRISE_FEATURES_ENABLED } from "./lib/constants";

const eePaths = [
  "/admin/groups",
  "/admin/api-key",
  "/admin/performance/usage",
  "/admin/performance/query-history",
  "/admin/whitelabeling",
  "/admin/performance/custom-analytics",
];

export async function middleware(request: NextRequest) {
  if (SERVER_SIDE_ONLY__PAID_ENTERPRISE_FEATURES_ENABLED) {
    const pathname = request.nextUrl.pathname;

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
    "/admin/groups/:path*",
    "/admin/api-key/:path*",
    "/admin/performance/usage/:path*",
    "/admin/performance/query-history/:path*",
    "/admin/whitelabeling/:path*",
    "/admin/performance/custom-analytics/:path*",
  ],
};
