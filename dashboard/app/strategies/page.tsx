import SectionShell from "../components/section-shell";
import StrategyManager from "./strategy-manager";

export default function StrategiesPage() {
  return <SectionShell active="/strategies/" eyebrow="MILESPAPA QUANT LAB · ATOMIC STRATEGIES" title="基本策略">
    <StrategyManager />
  </SectionShell>;
}
