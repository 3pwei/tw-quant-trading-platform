import SectionShell from "../components/section-shell";
import ReplayDashboard from "./replay-dashboard";

export default function ReplayPage() {
  return <SectionShell
    active="/replay/"
    eyebrow="MILESPAPA QUANT LAB · REPLAY"
    title="動態歷史回放"
  >
    <ReplayDashboard />
  </SectionShell>;
}
