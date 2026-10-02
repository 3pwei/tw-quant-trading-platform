import SectionShell from "../components/section-shell";
import HistoryDashboard from "./history-dashboard";

export default function HistoryPage() {
  return <SectionShell active="/history/" eyebrow="MILESPAPA QUANT LAB · HISTORY" title="回測／交易歷史">
    <HistoryDashboard />
  </SectionShell>;
}
