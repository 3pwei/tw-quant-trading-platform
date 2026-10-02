import { Suspense } from "react";
import SectionShell from "../../components/section-shell";
import CompositeBuilder from "../../strategies/composite-builder";

export default function CompositeStrategyEditorPage() {
  return <SectionShell active="/composite-strategies/" eyebrow="MILESPAPA QUANT LAB · COMPOSITE EDITOR" title="編輯組合策略">
    <Suspense fallback={<section className="panel strategy-loading">正在載入策略編輯器…</section>}><CompositeBuilder mode="edit" /></Suspense>
  </SectionShell>;
}
