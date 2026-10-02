const priceFormatter = new Intl.NumberFormat("zh-TW", {
  maximumFractionDigits: 2,
});
const decimalFormatter = new Intl.NumberFormat("zh-TW", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const moneyFormatter = new Intl.NumberFormat("zh-TW", {
  maximumFractionDigits: 0,
});
const taipeiDateTimeFormatter = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei",
  hour12: false,
});
const taipeiClockFormatter = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function formatPrice(
  value?: number | null,
  fallback = "—",
): string {
  return value == null ? fallback : priceFormatter.format(value);
}

export function formatDecimal(value: number): string {
  return decimalFormatter.format(value);
}

export function formatMoney(value: number): string {
  return moneyFormatter.format(value);
}

export function formatSignedMoney(value: number): string {
  return `${value >= 0 ? "+" : "−"}NT$ ${formatMoney(Math.abs(value))}`;
}

export function formatTaipeiDateTime(
  value?: string | null,
  fallback = "—",
): string {
  return value ? taipeiDateTimeFormatter.format(new Date(value)) : fallback;
}

export function formatTaipeiClock(
  value?: string | number | null,
  fallback = "—",
): string {
  if (value == null) return fallback;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return taipeiClockFormatter.format(date);
}
