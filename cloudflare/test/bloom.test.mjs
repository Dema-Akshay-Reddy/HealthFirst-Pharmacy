/**
 * Bloom filter unit tests (node:test, no deps).
 *   node --test test/bloom.test.mjs
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { BloomFilter } from "../src/bloom.js";

describe("BloomFilter sizing", () => {
  test("capacity 10k @ p=0.01 → ~95,851 bits, k=7, ~11.98 KiB", () => {
    const f = new BloomFilter({ capacity: 10_000, fpRate: 0.01 });
    // m = -n ln p / (ln2)^2 = 95850.5… ⇒ ceil(95851) bits
    assert.equal(f.sizeBits, 95851);
    assert.equal(f.hashCount, 7);
    assert.equal(f.bits.byteLength, Math.ceil(95851 / 8)); // 11982 bytes
  });

  test("tiny/invalid parameters are clamped safely", () => {
    const f = new BloomFilter({ capacity: 0, fpRate: 99 });
    assert.ok(f.sizeBits >= 64);
    assert.ok(f.hashCount >= 1);
    assert.equal(f.bits.byteLength, Math.ceil(f.sizeBits / 8));
  });
});

describe("membership semantics", () => {
  test("inserted values are always possible members", () => {
    const f = new BloomFilter({ capacity: 1000, fpRate: 0.01 });
    const keys = ["dolo 650", "telma 40", "pan 40", "azithral 500", "augmentin 625 duo"];
    for (const k of keys) f.add(k);
    for (const k of keys) assert.equal(f.mayContain(k), true, k);
  });

  test("repeated insertions are safe no-ops", () => {
    const f = new BloomFilter({ capacity: 100, fpRate: 0.01 });
    f.add("x");
    f.add("x"); f.add("x"); f.add("x");
    assert.equal(f.inserts, 4); // counted, but the bits stay identical
    assert.equal(f.mayContain("x"), true);
    const before = [...f.bits];
    f.add("x");
    assert.deepEqual([...f.bits], before);
  });

  test("key normalization: String() coercion, deterministic for '' / null", () => {
    const f = new BloomFilter();
    f.add("123");
    assert.equal(f.mayContain(123), true);
    assert.equal(f.mayContain("123"), true);
  });
});

describe("absence + false-positive behaviour", () => {
  test("empty filter: nothing 'may exist'", () => {
    const f = new BloomFilter({ capacity: 500 });
    assert.equal(f.mayContain("anything"), false);
  });

  test("empty filter never claims membership across many probes", () => {
    const f = new BloomFilter({ capacity: 1000, fpRate: 0.01 });
    for (let i = 0; i < 5000; i++) {
      assert.equal(f.mayContain(`absent-${i}`), false);
    }
  });

  test("absent values FP rate stays under ~3× target at half capacity", () => {
    const capacity = 2000;
    const f = new BloomFilter({ capacity, fpRate: 0.02 });
    for (let i = 0; i < capacity; i++) f.add(`key-${i}`); // 100% full
    let fp = 0;
    const N = 6000;
    for (let i = 0; i < N; i++) {
      if (f.mayContain(`probe-${i}`)) fp++;
    }
    const rate = fp / N;
    assert.ok(rate < 0.06, `observed ${rate} (target p=0.02)`);
  });

  test("estimatedFpRate() matches the (1-e^(-kn/m))^k formula", () => {
    const capacity = 1000, fp = 0.01;
    const f = new BloomFilter({ capacity, fpRate: fp });
    for (let i = 0; i < capacity; i++) f.add(`k${i}`);
    const { hashCount: k, sizeBits: m } = f;
    const expected = Math.pow(1 - Math.exp((-k * capacity) / m), k);
    assert.ok(Math.abs(f.estimatedFpRate() - expected) < 1e-9);
    assert.ok(f.estimatedFpRate() < 0.03);
  });
});

describe("determinism", () => {
  test("identical add sequences → identical bit arrays", () => {
    const build = () => {
      const f = new BloomFilter({ capacity: 300, fpRate: 0.005 });
      for (const k of ["a", "b", "c", "dolo 650", "telma 40"]) f.add(k);
      return [...f.bits];
    };
    assert.deepEqual(build(), build());
  });

  test("same query → same answer every call", () => {
    const f = new BloomFilter({ capacity: 100 });
    f.add("present");
    for (let i = 0; i < 50; i++) {
      assert.equal(f.mayContain("present"), true);
      assert.equal(f.mayContain("not-there"), false);
    }
  });
});

describe("stats()", () => {
  test("reports bytes, KiB, capacity and achieved estimate", () => {
    const f = new BloomFilter({ capacity: 10_000, fpRate: 0.01 });
    for (let i = 0; i < 10_000; i++) f.add(`med-${i}`);
    const s = f.stats();
    assert.equal(s.capacity, 10_000);
    assert.equal(s.hash_functions, 7);
    assert.equal(s.bytes, 11982);
    assert.equal(s.kib, Math.round((11982 / 1024) * 100) / 100);
    assert.equal(s.inserts, 10_000);
    assert.ok(s.estimated_fp_rate > 0 && s.estimated_fp_rate < 0.02);
  });
});
