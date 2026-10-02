/** Initial selections come only from the current server catalog. */
export function initialStrategyKeys(catalog: readonly { key: string }[], count = 1): string[] {
  return [...new Set(catalog.map(item => item.key).filter(Boolean))].slice(0, Math.max(0, count));
}

export type SchemaField = { name: string; kind: "string" | "integer" | "number" | "boolean"; required: boolean; title?: string; description?: string };
export type PublicStrategy = { key: string; name: string; parameters: Record<string, unknown>; parameter_schema: { schema_version: string; fields: SchemaField[] } };

export function parameterDraft(item: PublicStrategy): Record<string, string> {
  return Object.fromEntries(item.parameter_schema.fields.map(field => [field.name,
    field.name in item.parameters ? String(item.parameters[field.name]) : "",
  ]));
}

export function parseParameterDraft(item: PublicStrategy, draft: Record<string, string>): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const field of item.parameter_schema.fields) {
    const raw = draft[field.name];
    if (raw === undefined || raw === "") {
      if (field.required) throw new Error(`${field.title ?? field.name} 必須填寫`);
      continue;
    }
    if (field.kind === "string") values[field.name] = raw;
    else if (field.kind === "boolean") {
      if (raw !== "true" && raw !== "false") throw new Error("無效的布林值");
      values[field.name] = raw === "true";
    } else if (field.kind === "integer" || field.kind === "number") {
      const value = Number(raw);
      if (!raw.trim() || !Number.isFinite(value) || (field.kind === "integer" && !Number.isInteger(value))) throw new Error("無效的數值");
      values[field.name] = value;
    } else throw new Error("不支援的參數型別");
  }
  return values;
}
