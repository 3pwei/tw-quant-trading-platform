import { Suspense } from "react";
import SectionShell from "../../components/section-shell";
import CompositeBuilder from "../../strategies/composite-builder";

export default function NewCompositeStrategyPage() {
  return <SectionShell active="/composite-strategies/" eyebrow="MILESPAPA QUANT LAB · NEW COMPOSITE" title="建立組合策略">
    <Suspense fallback={<section className="panel strategy-loading">正在準備策略編輯器…</section>}><CompositeBuilder mode="new" /></Suspense>
  </SectionShell>;
}
