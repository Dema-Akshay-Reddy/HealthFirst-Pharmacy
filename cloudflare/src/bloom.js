/**
 * Reusable, deterministic Bloom filter + a catalog-name pre-check.
 *
 * Correctness rules (documented and enforced here):
 *   - mayContain() === false  ⇒ the key is DEFINITELY absent from the set of
 *     keys ever added to this filter, provided the filter is complete and
 *     current for the dataset.
 *   - mayContain() === true   ⇒ the key MAY exist — always confirm in D1
 *     before treating a positive as proof. A positive result is never proof.
 *
 * Sizing (standard formulas):
 *   m bits = -n * ln(p) / (ln 2)^2      — required bit-array size
 *   k      = (m / n) * ln 2             — number of hash functions
 * where n = expected capacity, p = target false-positive probability.
 *
 * Hashing: double hashing over two independently seeded 32-bit FNV-1a
 * hashes — h_i = (h1 + i*h2) mod m. Deterministic across processes and
 * platforms (identical to pharmacy/bloom.py), no randomness, no deps.
 * FNV-1a is not cryptographic; membership filters do not need to be.
 *
 * Memory: bits array = ceil(m/8) bytes. E.g. capacity 10_000 @ p=0.01
 * ⇒ m = 95_851 bits ⇒ 11.98 KiB, k = 7. Documented in stats().
 */

const FNV_PRIME = 0x01000193;
const SEED_A = 0x811c9dc5; // FNV offset basis
const SEED_B = 0x9747b28c; // second basis → decorrelated hash pair

/** 32-bit FNV-1a over UTF-16 code units (stable across engines). */
function fnv1a(str, seed) {
  let h = seed >>> 0;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, FNV_PRIME) >>> 0;
  }
  return h >>> 0;
}

export class BloomFilter {
  /**
   * @param {object} [opts]
   * @param {number} [opts.capacity=1000]   expected distinct keys (n)
   * @param {number} [opts.fpRate=0.01]     target false-positive rate (p)
   */
  constructor({ capacity = 1000, fpRate = 0.01 } = {}) {
    const n = Math.max(1, Math.floor(Number(capacity) || 1000));
    // p clamped to (1e-10, 0.5]: below that m explodes, above it is useless
    const p = Math.min(0.5, Math.max(1e-10, Number(fpRate) || 0.01));
    this.capacity = n;
    this.targetFpRate = p;
    this.sizeBits = Math.max(64, Math.ceil((-n * Math.log(p)) / (Math.LN2 * Math.LN2)));
    this.hashCount = Math.max(1, Math.round((this.sizeBits / n) * Math.LN2));
    this.bits = new Uint8Array(Math.ceil(this.sizeBits / 8));
    this.inserts = 0; // add() calls (repeats count once each — harmless)
  }

  /** Bit indices for a key — identical sequence every call (deterministic). */
  _indexes(key) {
    const h1 = fnv1a(key, SEED_A);
    const h2 = (fnv1a(key, SEED_B) | 1) >>> 0; // odd → better stride coverage
    const out = new Array(this.hashCount);
    for (let i = 0; i < this.hashCount; i++) {
      // Math.imul may return negative → force unsigned before % so every
      // index is a valid position in [0, sizeBits)
      out[i] = ((h1 + Math.imul(i, h2)) >>> 0) % this.sizeBits;
    }
    return out;
  }

  /** Insert a key. Repeated insertions of the same key are safe no-ops. */
  add(key) {
    const s = String(key ?? "");
    const idx = this._indexes(s);
    for (const i of idx) this.bits[i >> 3] |= 1 << (i & 7);
    this.inserts++;
    return this;
  }

  /** Bulk insert (iterable of keys). */
  addAll(keys) {
    for (const k of keys) this.add(k);
    return this;
  }

  /**
   * Membership check. false = definitely absent (when the filter is complete
   * and current); true = maybe present → confirm in the database.
   */
  mayContain(key) {
    const s = String(key ?? "");
    const idx = this._indexes(s);
    const bits = this.bits;
    for (const i of idx) {
      if ((bits[i >> 3] & (1 << (i & 7))) === 0) return false;
    }
    return true;
  }

  /** Current (post-fill) false-positive probability: (1 - e^(-kn/m))^k. */
  estimatedFpRate() {
    const { hashCount: k, sizeBits: m, inserts: n } = this;
    if (!n) return 0;
    const exponent = -1 * ((k * n) / m);
    const inner = 1 - Math.exp(exponent);
    return Math.pow(inner, k);
  }

  /** Observability snapshot — memory, capacity, achieved estimate. */
  stats() {
    const bytes = this.bits.byteLength;
    return {
      capacity: this.capacity,
      target_fp_rate: this.targetFpRate,
      size_bits: this.sizeBits,
      hash_functions: this.hashCount,
      bytes,
      kib: Math.round((bytes / 1024) * 100) / 100,
      inserts: this.inserts,
      estimated_fp_rate: Math.round(this.estimatedFpRate() * 1e6) / 1e6,
    };
  }
}

/* ------------------------------------------------------------------------ *
 * Catalog pre-check: one filter over drugs.norm_name, rebuilt lazily.
 *
 * The filter is trusted for negatives ONLY when fresh: it is invalidated on
 * any write to the drugs table (any isolate that captures such a write bumps
 * the version) and by TTL. Stale filters are rebuilt from D1 before their
 * negative answer is used, so a newly added medicine can never be reported
 * as "definitely absent".
 * ------------------------------------------------------------------------ */

const CATALOG_TTL_MS = 60_000;
const catalog = { filter: null, builtAt: 0, version: 0, builtVersion: -1 };

/** Called by the write layer whenever a captured statement touches a table. */
export function noteCatalogWrite(table) {
  if (table === "drugs") catalog.version++; // any isolate-local write
}

async function buildCatalogFilter(session) {
  const { results } = await session.prepare("SELECT norm_name FROM drugs").all();
  const f = new BloomFilter({ capacity: Math.max(64, (results?.length || 0) * 2), fpRate: 0.005 });
  for (const r of results || []) if (r.norm_name) f.add(r.norm_name);
  catalog.filter = f;
  catalog.builtAt = Date.now();
  catalog.builtVersion = catalog.version;
  return f;
}

/**
 * Fast pre-check for `SELECT * FROM drugs WHERE norm_name = ?`.
 * Returns:
 *   true  → may exist (STILL run the SQL query — confirm in D1)
 *   false → definitely absent (SQL exact-match query can be skipped)
 *   null  → no filter yet (caller runs the normal query)
 *
 * A false answer is only returned when the filter is fresh (TTL + version).
 * Any doubt → rebuild → if rebuild fails, return null (fail open: the DB
 * query runs; never let the filter deny a real record).
 */
export async function normNameMayExist(session, normName) {
  try {
    const stale = !catalog.filter
      || Date.now() - catalog.builtAt > CATALOG_TTL_MS
      || catalog.builtVersion !== catalog.version;
    if (stale) await buildCatalogFilter(session);
    return catalog.filter.mayContain(String(normName ?? "").toLowerCase());
  } catch {
    return null; // fail open — DB query still runs
  }
}

/** Stats for /api/meta observability. */
export function catalogBloomStats() {
  if (!catalog.filter) return { ready: false };
  return { ready: true, ...catalog.filter.stats() };
}
