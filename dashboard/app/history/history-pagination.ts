export type PageSlot =
  | { kind: "blank" }
  | { kind: "page"; number: number }
  | { kind: "previous" }
  | { kind: "next" }
  | { kind: "jump" };

const blank = (): PageSlot => ({ kind: "blank" });
const number = (value: number): PageSlot => ({ kind: "page", number: value });

export function historyPageSlots(page: number, totalPages: number): PageSlot[] {
  const last = Math.max(1, totalPages);
  const current = Math.max(1, Math.min(last, page));

  if (last <= 4) {
    const slots: PageSlot[] = Array.from(
      { length: Math.floor((9 - (last + 1)) / 2) }, blank,
    );
    for (let value = 1; value <= last; value++) slots.push(number(value));
    slots.push({ kind: "jump" });
    while (slots.length < 9) slots.push(blank());
    return slots;
  }
  if (current <= 4) {
    return [blank(), number(1), number(2), number(3), number(4),
      { kind: "next" }, number(last), { kind: "jump" }, blank()];
  }
  if (current >= last - 3) {
    return [blank(), number(1), { kind: "previous" },
      number(last - 3), number(last - 2), number(last - 1), number(last),
      { kind: "jump" }, blank()];
  }
  return [number(1), { kind: "previous" }, number(current - 1),
    number(current), number(current + 1), number(current + 2),
    { kind: "next" }, number(last), { kind: "jump" }];
}
