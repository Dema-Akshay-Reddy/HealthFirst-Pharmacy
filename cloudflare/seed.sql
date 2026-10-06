-- Minimal demo seed for the Cloudflare deployment (same shape as the Python
-- app's seeded DB). Run:  wrangler d1 execute pharmacy-primary --file=seed.sql
INSERT OR IGNORE INTO suppliers(id, name, email, lead_time_days, created_at) VALUES
  (1, 'Apollo Supply Chain', 'orders@apollosupplychain.com', 5, datetime('now')),
  (2, 'Hetero Healthcare',  'orders@heterohealthcare.com',  7, datetime('now')),
  (3, 'MedPlus Mart',       'orders@medplusmart.com',       6, datetime('now'));

INSERT OR IGNORE INTO shelves(id, code, zone, note) VALUES
  (1, 'P1',  'pick',       'Pick face — dispense patient medicines from here'),
  (2, 'P2',  'pick',       'Pick face'),
  (3, 'P3',  'pick',       'Pick face'),
  (4, 'R1',  'reserve',    'Reserve stock — refill pick faces from here'),
  (5, 'R2',  'reserve',    'Reserve stock'),
  (6, 'R3',  'reserve',    'Reserve stock'),
  (7, 'R4',  'reserve',    'Reserve stock'),
  (8, 'Q1',  'quarantine', 'Expired / blocked — stage for vendor return'),
  (9, 'RT1', 'returns',    'Picked, awaiting vendor pickup');

INSERT OR IGNORE INTO drugs(id, name, norm_name, generic, category, form, schedule, unit, mrp, cost, lead_time_days, supplier_id, created_at) VALUES
  (1, 'Dolo 650',    'dolo 650',    'Paracetamol',   'Analgesic & Antipyretic', 'Tablet', 'OTC',        'unit', 35.0,  22.0, 5, 1, datetime('now')),
  (2, 'Pan 40',      'pan 40',      'Pantoprazole',  'Gastrointestinal',        'Tablet', 'Schedule H', 'unit', 155.0, 118.0, 7, 1, datetime('now')),
  (3, 'Glycomet 500','glycomet 500','Metformin',     'Antidiabetic',            'Tablet', 'Schedule H', 'unit', 22.5,  15.0, 6, 2, datetime('now')),
  (4, 'Telma 40',    'telma 40',    'Telmisartan',   'Cardiovascular',          'Tablet', 'Schedule H', 'unit', 220.0, 165.0, 7, 3, datetime('now')),
  (5, 'Allegra 120', 'allegra 120', 'Fexofenadine',  'Antiallergic',            'Tablet', 'OTC',        'unit', 198.0, 148.0, 6, 2, datetime('now')),
  (6, 'Azithral 500','azithral 500','Azithromycin',  'Antibiotic',              'Tablet', 'Schedule H1','unit', 118.5, 88.0,  7, 3, datetime('now'));

-- batches: the nearest-expiry batch of each medicine sits on its pick shelf
INSERT OR IGNORE INTO batches(id, drug_id, supplier_id, batch_no, expiry_date, qty_received, qty_remaining, unit_cost, received_date, source, shelf_id, created_at) VALUES
  (1, 1, 1, 'DOL-2410-18', '2026-10-13', 800, 643, 22.0, '2024-10-18', 'seed', 1, datetime('now')),
  (2, 1, 1, 'DOL-2503-44', '2027-03-12', 900, 812, 22.0, '2025-03-12', 'seed', 4, datetime('now')),
  (3, 2, 1, 'PAN-2410-74', '2026-10-13', 700, 608, 118.0, '2024-10-18', 'seed', 1, datetime('now')),
  (4, 2, 2, 'PAN-2504-10', '2027-04-06', 800, 733, 118.0, '2025-04-10', 'seed', 4, datetime('now')),
  (5, 3, 2, 'GLY-2411-02', '2026-11-08', 600, 501, 15.0, '2024-11-02', 'seed', 2, datetime('now')),
  (6, 4, 3, 'TEL-2410-55', '2026-10-20', 700, 590, 165.0, '2024-10-20', 'seed', 2, datetime('now')),
  (7, 5, 2, 'ALL-2412-11', '2026-12-05', 650, 540, 148.0, '2024-12-11', 'seed', 3, datetime('now')),
  (8, 6, 3, 'AZI-2410-99', '2026-10-25', 750, 630, 88.0,  '2024-10-25', 'seed', 3, datetime('now'));

-- one expired lot per SKU so the shift-to-quarantine directive has work to do
INSERT OR IGNORE INTO batches(id, drug_id, supplier_id, batch_no, expiry_date, qty_received, qty_remaining, unit_cost, received_date, source, shelf_id, created_at) VALUES
  (9,  1, 1, 'DOL-2301-05', '2024-01-20', 300, 250, 20.0, '2023-01-20', 'seed', 8, datetime('now')),
  (10, 2, 1, 'PAN-2302-15', '2024-02-15', 280, 233, 110.0, '2023-02-15', 'seed', 8, datetime('now')),
  (11, 3, 2, 'GLY-2303-25', '2024-03-25', 260, 214, 14.0, '2023-03-25', 'seed', 8, datetime('now')),
  (12, 4, 3, 'TEL-2304-35', '2024-04-30', 270, 222, 160.0, '2023-04-30', 'seed', 8, datetime('now')),
  (13, 5, 2, 'ALL-2305-45', '2024-05-28', 250, 205, 140.0, '2023-05-28', 'seed', 8, datetime('now')),
  (14, 6, 3, 'AZI-2306-55', '2024-06-30', 240, 196, 85.0,  '2023-06-30', 'seed', 8, datetime('now'));

INSERT OR IGNORE INTO sales_daily(drug_id, date, qty, revenue) VALUES
  (1, date('now', '-1 day'), 8, 280.0), (2, date('now', '-1 day'), 3, 465.0),
  (3, date('now', '-1 day'), 4, 90.0),  (6, date('now', '-1 day'), 2, 237.0);

INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES
  ('pharmacy_name', 'HealthFirst Pharmacy', datetime('now')),
  ('service_level', '0.95', datetime('now')),
  ('expiry_critical_days', '30', datetime('now')),
  ('expiry_warning_days', '90', datetime('now'));
