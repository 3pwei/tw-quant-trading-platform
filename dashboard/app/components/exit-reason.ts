const EXIT_REASON_LABELS: Readonly<Record<string, string>> = {
  stop_loss: "停損",
  take_profit: "停利",
  force_exit: "時段平倉",
  session_end: "時段結束",
  end_of_data: "資料結束",
  mean_reversion: "回歸均線",
  opposite_signal: "反向訊號",
  channel_midpoint: "跌破／突破通道中線",
  channel_invalidation: "Dow 軌道失效",
  contract_roll: "合約換月",
  strategy_exit: "策略出場",
};

export function exitReasonLabel(reason: string): string {
  return EXIT_REASON_LABELS[reason] ?? reason;
}
