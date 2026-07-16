import type { MetadataRoute } from "next";

// Web App Manifest — App Router serves this at `/manifest.webmanifest` and
// auto-injects `<link rel="manifest">` into <head>. This alone makes Darwin
// installable as a standalone app (the "install" icon in Chrome/Edge, "Add to
// Dock" in Safari 17+); modern Chromium no longer requires a service worker for
// installability, so we don't ship one.
//
// Colors match the app's dark-default theme (globals.css `--background: #0f1117`).
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Darwin",
    short_name: "Darwin",
    description: "Question answering for your documents",
    // Open straight into chat; if unauthenticated the app hits /auth/login and
    // the SSO next-preservation restores this destination after sign-in.
    start_url: "/chat",
    scope: "/",
    display: "standalone",
    orientation: "portrait-primary",
    background_color: "#0f1117",
    theme_color: "#0f1117",
    icons: [
      {
        src: "/icons/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
