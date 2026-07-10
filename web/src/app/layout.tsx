import "./globals.css";

import { IBM_Plex_Sans } from "next/font/google";
import { getCombinedSettings } from "@/components/settings/lib";
import { CUSTOM_ANALYTICS_ENABLED } from "@/lib/constants";
import { SettingsProvider } from "@/components/settings/SettingsProvider";
import { Metadata, Viewport } from "next";
import { buildClientUrl } from "@/lib/utilsSS";

// Body / UI: IBM Plex Sans — a refined, characterful humanist sans (not Inter).
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
});

export async function generateMetadata(): Promise<Metadata> {
  const dynamicSettings = await getCombinedSettings({ forceRetrieval: true });
  const logoLocation =
    dynamicSettings.enterpriseSettings &&
    dynamicSettings.enterpriseSettings?.use_custom_logo
      ? "/api/enterprise-settings/logo"
      : buildClientUrl("/favicon.ico");

  return {
    title: dynamicSettings.enterpriseSettings?.application_name ?? "Darwin",
    description: "Question answering for your documents",
    // App Router auto-injects <link rel="manifest"> from app/manifest.ts.
    icons: {
      icon: logoLocation,
      apple: "/icons/apple-touch-icon.png",
    },
  };
}

// PWA / mobile chrome: match the dark-default theme (globals.css --background).
export const viewport: Viewport = {
  themeColor: "#0f1117",
};

export const dynamic = "force-dynamic";

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const combinedSettings = await getCombinedSettings({});

  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        {/* Dark is the server-rendered default (className="dark" above), so it
            survives React hydration. This pre-paint script only OPTS OUT for an
            explicit "light" choice — removing the class before paint (no flash).
            (Previously the script *added* dark to a class-less <html>, which
            React then clobbered on hydration, leaving prod stuck in light.) */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{if(localStorage.getItem('darwin-theme')==='light'){document.documentElement.classList.remove('dark')}}catch(e){}`,
          }}
        />
        {CUSTOM_ANALYTICS_ENABLED && combinedSettings.customAnalyticsScript && (
          <script
            type="text/javascript"
            dangerouslySetInnerHTML={{
              __html: combinedSettings.customAnalyticsScript,
            }}
          />
        )}
      </head>
      <body
        className={`${plexSans.variable} font-sans text-default bg-background`}
      >
        <SettingsProvider settings={combinedSettings}>
          {children}
        </SettingsProvider>
      </body>
    </html>
  );
}
