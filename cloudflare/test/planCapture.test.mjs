/* Verifies planCapture classifies EVERY write() SQL shape used in src/. */
import { planCapture } from "../src/replication.js";

const q = (s) => s.replace(/'/g, "\\'");
const D = "datetime('now')";
const cases = [
  ["INSERT INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))", 5, "insert", "users"],
  [`INSERT INTO reorders(drug_id, supplier_id, qty, due_date, reason, status, source, notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,${D},${D})`, 8, "insert", "reorders"],
  [`INSERT INTO notifications(supplier_id, subject, body, status, related_type, related_id, created_at) VALUES(?,?,?,?,?,?,${D})`, 6, "insert", "notifications"],
  ["UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ?", 2, "update", "batches"],
  [`INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, status, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,${D})`, 9, "insert", "waste"],
  [`INSERT INTO returns(supplier_id, reference, qty, value, waste_ids, status, note, created_at) VALUES(?,?,?,?,?,?,?,${D})`, 7, "insert", "returns"],
  ["UPDATE waste SET status='return_requested' WHERE id IN (?,?)", 2, "update", "waste"],
  ["INSERT INTO settings(key, value, updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", 2, "insert", "settings"],
  ["UPDATE alerts SET status='acknowledged', updated_at=? WHERE id=?", 2, "update", "alerts"],
  ["UPDATE notifications SET status=?, sent_at=? WHERE id=?", 3, "update", "notifications"],
  ["UPDATE reorders SET status=?, updated_at=? WHERE id=?", 3, "update", "reorders"],
  ["UPDATE returns SET status=?, updated_at=? WHERE id=?", 3, "update", "returns"],
  ["UPDATE waste SET status=? WHERE id IN (?,?,?)", 4, "update", "waste"],
  ["UPDATE drugs SET lead_time_days = ? WHERE id = ?", 2, "update", "drugs"],
  ["UPDATE drugs SET reorder_point = ? WHERE id = ?", 2, "update", "drugs"],
  ["UPDATE drugs SET supplier_id = ? WHERE id = ?", 2, "update", "drugs"],
  ["UPDATE alerts SET status='acknowledged', updated_at=? WHERE status='active'", 1, "update", "alerts"],
  ["INSERT INTO sales(txn_id, date, drug_id, qty, unit_price, total, source, created_at) VALUES(?,?,?,?,?,?,?,?)", 8, "insert", "sales"],
  ["INSERT INTO bills(bill_no, date, patient, doctor, note, items_count, units, amount, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", 10, "insert", "bills"],
  ["INSERT INTO bill_items(bill_id, drug_id, drug_name, batch_no, qty, unit_price, line_total, created_at) VALUES(?,?,?,?,?,?,?,?)", 8, "insert", "bill_items"],
  ["UPDATE shelf_tasks SET status = 'done', done_at = datetime('now') WHERE id = ?", 1, "update", "shelf_tasks"],
  ["UPDATE batches SET shelf_id = ? WHERE id = ?", 2, "update", "batches"],
  ["UPDATE batches SET shelf_id = ? WHERE drug_id = ? AND qty_remaining > 0 AND expiry_date IS NOT NULL AND expiry_date < date('now')", 2, "update", "batches"],
  ["INSERT INTO shelf_tasks(kind, drug_id, batch_id, from_shelf_id, to_shelf_id, reason, status, created_at) SELECT 'shift', ?, ?, ?, ?, ?, 'pending', ? WHERE NOT EXISTS (SELECT 1 FROM shelf_tasks WHERE batch_id = ? AND to_shelf_id = ? AND status = 'pending')", 8, "insert", "shelf_tasks"],
  ["UPDATE alerts SET severity = ?, title = ?, message = ?, details = ?, drug_id = ?, batch_id = ?, updated_at = ? WHERE dedup_key = ?", 8, "update", "alerts"],
  ["INSERT INTO alerts(atype, severity, drug_id, batch_id, title, message, details, dedup_key, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)", 10, "insert", "alerts"],
  ["UPDATE alerts SET status = 'resolved', updated_at = ? WHERE dedup_key = ?", 2, "update", "alerts"],
  ["INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))", 4, "insert", "users"],
  ["INSERT INTO settings(key, value, updated_at) VALUES('session_secret',?,datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value", 1, "insert", "settings"],
  ["INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, status, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", 10, "insert", "waste"],
  ["INSERT INTO archive_runs(period, key, tables_count, rows_count, size_bytes, status, error, created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))", 7, "insert", "archive_runs"],
  ["INSERT INTO sync_bookmarks(name, bookmark, updated_at) VALUES(?,?,datetime('now')) ON CONFLICT(name) DO UPDATE SET bookmark=excluded.bookmark, updated_at=excluded.updated_at", 2, "none", "sync_bookmarks"],
];

let fails = 0;
for (const [sql, n, wantKind, wantTable] of cases) {
  const p = planCapture(sql, new Array(n).fill(1));
  if (p.kind !== wantKind || p.table !== wantTable) {
    fails++;
    console.log(`FAIL [${wantKind}/${wantTable}] got [${p.kind}/${p.table}] ${p.reason || ""} :: ${sql.slice(0, 80)}`);
  }
}
console.log(fails === 0 ? `ALL ${cases.length} write() shapes classified correctly` : `${fails} FAILURES`);
process.exit(fails ? 1 : 0);
