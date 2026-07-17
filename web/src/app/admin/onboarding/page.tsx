import { AdminPageTitle } from "@/components/admin/Title";
import { RobotIcon } from "@/components/icons/icons";
import { OnboardingRequestsTable } from "./OnboardingRequestsTable";

export default function Page() {
  return (
    <div className="mx-auto container">
      <AdminPageTitle
        icon={<RobotIcon size={32} />}
        title="Onboarding requests"
      />
      <p className="text-sm text-subtle mb-4">
        Approve a request to auto-provision the team&apos;s assistant, sources
        (scraped with bumped priority), and Slack bot config.
      </p>
      <OnboardingRequestsTable />
    </div>
  );
}
