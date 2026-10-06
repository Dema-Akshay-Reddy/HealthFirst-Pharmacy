/**
 * Shared reorder-plan maths — mirrors pharmacy/forecasting.py using a
 * lead-time demand + 3-day safety stock approximation on 90-day sales history.
 */
export const todayStr = () => new Date().toISOString().slice(0, 10);
export const addDays = (iso, n) => new Date(new Date(iso + "T00:00:00Z").getTime() + n * 86400000).toISOString().slice(0, 10);

export function computePlan(d, avgDaily, today) {
  const lead = Number(d.lead_time_days || 7);
  const demandLt = avgDaily * lead;
  const safety = Math.ceil(avgDaily * 3);
  const manual = (d.reorder_point === null || d.reorder_point === undefined || d.reorder_point === "")
    ? null : Number(d.reorder_point);
  const rop = manual ?? Math.ceil(demandLt + safety);
  const orderQty = Math.max(0, Math.ceil(rop - d.usable + safety));
  const cover = avgDaily > 0 ? Math.round((d.usable / avgDaily) * 10) / 10 : 9999;
  const status = d.usable < rop ? "order_now" : (avgDaily > 0 && cover <= 90 ? "scheduled" : "healthy");
  const due = status === "order_now" ? today
    : (status === "scheduled" && avgDaily > 0
      ? addDays(today, Math.max(0, Math.floor((d.usable - rop) / avgDaily))) : null);
  return {
    available: d.usable, expired_stock: d.expired, available_value: d.usable_value,
    lead_time_days: lead, demand_lead_time: Math.round(demandLt * 10) / 10, safety_stock: safety,
    reorder_point: rop, reorder_point_auto: Math.ceil(demandLt + safety), manual_override: manual !== null,
    order_qty: orderQty, status, due_date: due, cover_days: cover, avg_daily: avgDaily,
  };
}
