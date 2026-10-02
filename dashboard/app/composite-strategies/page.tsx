import SectionShell from "../components/section-shell";
import CompositeCatalog from "./composite-catalog";

export default function CompositeStrategiesPage() {
  return <SectionShell active="/composite-strategies/" eyebrow="MILESPAPA QUANT LAB · COMPOSITE STRATEGIES" title="組合策略">
    <CompositeCatalog />
  </SectionShell>;
}
