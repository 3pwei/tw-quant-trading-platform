export type StrategyMenuElement = Pick<
  HTMLDetailsElement,
  "contains" | "open"
>;

export function closeStrategyMenuWhenOutside(
  menu: StrategyMenuElement | null,
  target: EventTarget | null,
): boolean {
  if (!menu?.open || target == null || menu.contains(target as Node)) {
    return false;
  }
  menu.open = false;
  return true;
}
