import "./globals.css";

import { IBM_Plex_Sans, Fraunces } from "next/font/google";
import { getCombinedSettings } from "@/components/settings/lib";
import { CUSTOM_ANALYTICS_ENABLED } from "@/lib/constants";
import { SettingsProvider } from "@/components/settings/SettingsProvider";
import { Metadata } from "next";
import { buildClientUrl } from "@/lib/utilsSS";

// Body / UI: IBM Plex Sans — a refined, characterful humanist sans (not Inter).
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
});
// Display: Fraunces — a warm, high-contrast serif for the assistant identity
// and empty-state headline. Gives the product an editorial point of view.
const fraunces = Fraunces({
  subsets: ["latin"],
  variable: "--font-display",
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
    icons: {
      icon: logoLocation,
    },
  };
}

export const dynamic = "force-dynamic";

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const combinedSettings = await getCombinedSettings({});

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* No-flash theme init: dark is the default everywhere; only an explicit
            "light" choice opts out. Runs before paint so there's no flash. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{if(localStorage.getItem('darwin-theme')!=='light'){document.documentElement.classList.add('dark')}}catch(e){document.documentElement.classList.add('dark')}`,
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
        className={`${plexSans.variable} ${fraunces.variable} font-sans text-default bg-background`}
      >
        <SettingsProvider settings={combinedSettings}>
          {children}
        </SettingsProvider>
      </body>
    </html>
  );
}
