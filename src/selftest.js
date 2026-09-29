'use strict';
/**
 * 自测：单元测试（profiler / counters / 统计函数）+ 边界用例 + 多线程集成测试。
 * 用法: node src/selftest.js   失败时以非零码退出。
 */
const path = require('path');
const os = require('os');
const { Worker } = require('worker_threads');
const {
  CacheLineProfiler, SLOTS_PER_LINE, kendallTau, topKOverlap,
} = require('./profiler.js');
const { PackedCounters, PaddedCounters, NaiveSharedCounter, ShardedCounter } = require('./counters.js');

const WORKER = path.join(__dirname, 'bench_worker.js');
let passed = 0, failed = 0;

function check(name, cond) {
  if (cond) { passed++; console.log(`  ok   ${name}`); }
  else { failed++; console.error(`  FAIL ${name}`); }
}
function eq(name, a, b) { check(`${name} (${a} === ${b})`, a === b); }

function runWorkers(configs) {
  return new Promise((resolve, reject) => {
    let done = 0;
    const results = new Array(configs.length);
    for (let i = 0; i < configs.length; i++) {
      const w = new Worker(WORKER, { workerData: configs[i] });
      w.once('message', (msg) => { results[i] = msg; if (++done === configs.length) resolve(results); });
      w.once('error', reject);
    }
  });
}

function testLineMapping() {
  console.log('[line mapping]');
  eq('slot 0 -> line 0', CacheLineProfiler.lineOf(0), 0);
  eq('slot 15 -> line 0', CacheLineProfiler.lineOf(SLOTS_PER_LINE - 1), 0);
  eq('slot 16 -> line 1', CacheLineProfiler.lineOf(SLOTS_PER_LINE), 1);
  eq('slot 63 -> line 3', CacheLineProfiler.lineOf(63), 3);
}

function testProfilerSingleThread() {
  console.log('[profiler: single thread, no contention]');
  const p = new CacheLineProfiler(4, 1);
  for (let i = 0; i < 100; i++) p.recordWrite(0, 0);
  for (let i = 0; i < 50; i++) p.recordWrite(3, 0);
  const vars = p.perVariable();
  eq('var0 totalWrites', vars[0].totalWrites, 100);
  eq('var3 totalWrites', vars[3].totalWrites, 50);
  eq('var0 foreignWrites (single thread => 0)', vars[0].foreignWrites, 0);
  eq('var0 writers', vars[0].writers, 1);
  const lines = p.perLine();
  eq('line0 totalWrites (vars 0..3 same line)', lines[0].totalWrites, 150);
  eq('line0 foreignWrites', lines[0].foreignWrites, 0);
}

function testProfilerMultiThread() {
  console.log('[profiler: two threads, synthetic contention]');
  const p = new CacheLineProfiler(4, 2);
  for (let i = 0; i < 100; i++) p.recordWrite(0, 0); // t0 是 var0 属主
  for (let i = 0; i < 40; i++) p.recordWrite(0, 1);  // t1 越界写 var0
  for (let i = 0; i < 10; i++) p.recordWrite(1, 1);  // t1 独占 var1（与 var0 同行）
  const vars = p.perVariable();
  eq('var0 foreignWrites', vars[0].foreignWrites, 40);
  eq('var0 writers', vars[0].writers, 2);
  eq('var1 foreignWrites (owned by t1)', vars[1].foreignWrites, 0);
  const lines = p.perLine();
  eq('line0 totalWrites', lines[0].totalWrites, 150);
  eq('line0 foreignWrites (t1 的 40+10 全是非属主写)', lines[0].foreignWrites, 50);
  const rank = p.ranking('variable');
  eq('rank1 is var0', rank[0].unit, 0);
  eq('ranking excludes untouched vars', rank.length, 2);
}

function testStatsUtils() {
  console.log('[ranking stability utils]');
  eq('kendall identical', kendallTau([1, 2, 3], [1, 2, 3]), 1);
  eq('kendall reversed', kendallTau([1, 2, 3], [3, 2, 1]), -1);
  eq('topK identical', topKOverlap([1, 2, 3], [1, 2, 3], 2), 1);
  eq('topK disjoint', topKOverlap([1, 2], [3, 4], 1), 0);
  eq('topK half', topKOverlap([1, 2], [2, 3], 1), 0);
}

function testCounterLayouts() {
  console.log('[counter layouts]');
  const packed = new PackedCounters(4);
  eq('packed slots adjacent', packed.slot(1) - packed.slot(0), 1);
  const padded = new PaddedCounters(4);
  eq('padded stride = one cache line', padded.slot(1) - padded.slot(0), SLOTS_PER_LINE);
  const sharded = new ShardedCounter(3);
  eq('sharded stride = one cache line', sharded.slot(1) - sharded.slot(0), SLOTS_PER_LINE);
  const naive = new NaiveSharedCounter();
  const view = new Int32Array(naive.sab);
  for (let i = 0; i < 7; i++) Atomics.add(view, naive.slot(), 1);
  eq('naive shared counter value', naive.value(), 7);
  const sv = new Int32Array(sharded.sab);
  sv[sharded.slot(0)] = 3; sv[sharded.slot(2)] = 4;
  eq('sharded value sums shards', sharded.value(), 7);
}

function testEdgeCases() {
  console.log('[edge cases]');
  let threw = false;
  try { new CacheLineProfiler(0, 1); } catch (e) { threw = e instanceof RangeError; }
  check('profiler rejects numVars=0', threw);
  threw = false;
  try { new CacheLineProfiler(1, 0); } catch (e) { threw = e instanceof RangeError; }
  check('profiler rejects numThreads=0', threw);
  const p = new CacheLineProfiler(2, 2);
  eq('empty profiler ranking is empty', p.ranking('line').length, 0);
  eq('kendall of empty rankings', kendallTau([], []), 1);
  eq('topK of empty rankings', topKOverlap([], [], 3), 1);
}

async function testWorkerCounterCorrectness() {
  console.log('[integration: worker counter correctness]');
  const T = 4, ITERS = 100000;
  for (const [name, C] of [['packed', PackedCounters], ['padded', PaddedCounters]]) {
    const c = new C(T);
    const cfgs = [];
    for (let t = 0; t < T; t++) cfgs.push({ sab: c.sab, slot: c.slot(t), iterations: ITERS });
    await runWorkers(cfgs);
    let okAll = true;
    for (let t = 0; t < T; t++) if (c.value(t) !== ITERS) okAll = false;
    check(`${name}: every counter == iterations`, okAll);
  }
  // true sharing: naive atomic vs sharded，最终值都必须等于 T*ITERS
  const naive = new NaiveSharedCounter();
  await runWorkers(Array.from({ length: T }, () => ({ sab: naive.sab, slot: 0, iterations: ITERS, atomic: true })));
  eq('naive atomic total', naive.value(), T * ITERS);
  const sharded = new ShardedCounter(T);
  await runWorkers(Array.from({ length: T }, (_, t) => ({ sab: sharded.sab, slot: sharded.slot(t), iterations: ITERS })));
  eq('sharded total', sharded.value(), T * ITERS);
}

async function testSparseAndZero() {
  console.log('[integration: sparse writes & zero iterations]');
  const T = 4, ITERS = 100000, EVERY = 1000;
  const c = new PackedCounters(T);
  await runWorkers(Array.from({ length: T }, (_, t) => ({ sab: c.sab, slot: c.slot(t), iterations: ITERS, writeEvery: EVERY })));
  const expected = Math.ceil(ITERS / EVERY); // i=0,1000,2000,...
  let okAll = true;
  for (let t = 0; t < T; t++) if (c.value(t) !== expected) okAll = false;
  check(`sparse: each counter == ${expected}`, okAll);
  const z = new PackedCounters(2);
  const res = await runWorkers([{ sab: z.sab, slot: 0, iterations: 0 }, { sab: z.sab, slot: 1, iterations: 0 }]);
  eq('zero iterations: ops=0', res[0].ops, 0);
  eq('zero iterations: counter stays 0', z.value(0), 0);
}

async function testOversubscription() {
  console.log('[integration: threads >> cores]');
  const T = os.cpus().length * 4, ITERS = 20000;
  const c = new PaddedCounters(T);
  await runWorkers(Array.from({ length: T }, (_, t) => ({ sab: c.sab, slot: c.slot(t), iterations: ITERS })));
  let okAll = true;
  for (let t = 0; t < T; t++) if (c.value(t) !== ITERS) okAll = false;
  check(`oversubscribed (${T} threads): all counters correct`, okAll);
}

async function testRankingStability() {
  console.log('[integration: contention ranking stability across runs]');
  const V = 64, T = 8, REPS = 5;
  const planFor = (t) => [[t * 8, 20000 + (t % 4) * 10000], [5, 5000]];
  const lineRanks = [], varRanks = [];
  for (let r = 0; r < REPS; r++) {
    const counters = new PackedCounters(V);
    const prof = new CacheLineProfiler(V, T);
    const cfgs = [];
    for (let t = 0; t < T; t++) {
      cfgs.push({ mode: 'profiled', sab: counters.sab, profSab: prof.counts.buffer,
                  numThreads: T, threadId: t, plan: planFor(t) });
    }
    await runWorkers(cfgs);
    lineRanks.push(prof.ranking('line').map((x) => x.unit));
    varRanks.push(prof.ranking('variable').map((x) => x.unit));
    if (r === REPS - 1) {
      const lr = prof.ranking('line');
      eq('line 0 is the most contended', lr[0].unit, 0);
      eq('line 0 writers == T', lr[0].writers, T);
      const vr = prof.ranking('variable');
      eq('var 5 (hot shared) is the most contended variable', vr[0].unit, 5);
      eq('var 5 writers == T', vr[0].writers, T);
      eq('var 5 foreignWrites', vr[0].foreignWrites, 5000 * (T - 1));
    }
  }
  let stable = true;
  for (let r = 1; r < REPS; r++) {
    if (kendallTau(lineRanks[r - 1], lineRanks[r]) !== 1) stable = false;
    if (topKOverlap(varRanks[r - 1], varRanks[r], 3) !== 1) stable = false;
  }
  check(`ranking identical across ${REPS} runs (line tau=1, var top3 overlap=1)`, stable);
}

async function main() {
  testLineMapping();
  testProfilerSingleThread();
  testProfilerMultiThread();
  testStatsUtils();
  testCounterLayouts();
  testEdgeCases();
  await testWorkerCounterCorrectness();
  await testSparseAndZero();
  await testOversubscription();
  await testRankingStability();
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });
