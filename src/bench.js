'use strict';
/**
 * 基准与争用画像驱动程序。
 * 用法: node src/bench.js [--reps N] [--iters N]
 */
const os = require('os');
const path = require('path');
const { Worker } = require('worker_threads');
const { performance } = require('perf_hooks');
const { CacheLineProfiler, kendallTau, topKOverlap } = require('./profiler.js');
const { PackedCounters, PaddedCounters, NaiveSharedCounter, ShardedCounter } = require('./counters.js');

const WORKER = path.join(__dirname, 'bench_worker.js');
const CPUS = os.cpus().length;

function parseArgs() {
  const args = { reps: 3, iters: 2_000_000 };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--reps') args.reps = +argv[++i];
    else if (argv[i] === '--iters') args.iters = +argv[++i];
  }
  return args;
}

function runWorkers(configs) {
  return new Promise((resolve, reject) => {
    let done = 0;
    const results = new Array(configs.length);
    const t0 = performance.now();
    for (let i = 0; i < configs.length; i++) {
      const w = new Worker(WORKER, { workerData: configs[i] });
      w.once('message', (msg) => {
        results[i] = msg;
        if (++done === configs.length) {
          resolve({ results, wallNanos: (performance.now() - t0) * 1e6 });
        }
      });
      w.once('error', reject);
    }
  });
}

function percentile(sorted, p) {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx];
}

/** 聚合一次运行的结果：吞吐(Mops/s)、平均/分位时延(ns/op)。 */
function summarize(results, wallNanos, batchSize) {
  const totalOps = results.reduce((s, r) => s + r.ops, 0);
  const batches = [];
  for (const r of results) for (const b of r.batches) batches.push(b);
  batches.sort((a, b) => a - b);
  const perOp = batches.map((b) => b / batchSize);
  const latAvg = results.reduce((s, r) => s + (r.ops > 0 ? r.nanos / r.ops : 0), 0) / results.length;
  return {
    ops: totalOps,
    wallMs: wallNanos / 1e6,
    mops: totalOps / (wallNanos / 1e3), // ops / (ns) * 1e3 = Mops/s
    latAvgNs: latAvg, // 各工作线程 每次写平均耗时 的均值
    latP50Ns: percentile(perOp, 50),
    latP99Ns: percentile(perOp, 99),
  };
}

function meanStd(xs) {
  const m = xs.reduce((a, b) => a + b, 0) / xs.length;
  const sd = Math.sqrt(xs.reduce((s, x) => s + (x - m) ** 2, 0) / xs.length);
  return { mean: m, std: sd };
}

const BATCH = 4096;

/** 跑一组（1 轮预热 + reps 轮计时），返回聚合统计。 */
async function benchVariant(makeCfg, threads, iters, reps, extra = {}) {
  const runs = [];
  for (let r = -1; r < reps; r++) { // r=-1 为预热轮，不计入统计

    const ctx = extra.makeShared ? extra.makeShared() : {};
    const configs = [];
    for (let t = 0; t < threads; t++) configs.push(makeCfg(t, ctx));
    const { results, wallNanos } = await runWorkers(configs);
    if (r >= 0) runs.push(summarize(results, wallNanos, BATCH));
    if (extra.check) extra.check(ctx, threads, iters);
  }
  const mops = meanStd(runs.map((x) => x.mops));
  const p50 = meanStd(runs.map((x) => x.latP50Ns));
  const p99 = meanStd(runs.map((x) => x.latP99Ns));
  return { mops, p50, p99 };
}

function fmt(x, d = 2) { return x.toFixed(d); }

async function scenarioFalseSharing(threads, iters, reps, label) {
  const packed = await benchVariant(
    (t, ctx) => ({ sab: ctx.c.sab, slot: ctx.c.slot(t), iterations: iters }),
    threads, iters, reps,
    { makeShared: () => ({ c: new PackedCounters(threads) }),
      check: (ctx, th, it) => { for (let t = 0; t < th; t++) assertEq(ctx.c.value(t), it, 'packed value'); } }
  );
  const padded = await benchVariant(
    (t, ctx) => ({ sab: ctx.c.sab, slot: ctx.c.slot(t), iterations: iters }),
    threads, iters, reps,
    { makeShared: () => ({ c: new PaddedCounters(threads) }),
      check: (ctx, th, it) => { for (let t = 0; t < th; t++) assertEq(ctx.c.value(t), it, 'padded value'); } }
  );
  printComparison(label, threads, packed, padded, 'packed(基线)', 'padded(对齐填充)');
  return { label, threads, baseline: packed, mitigated: padded };
}

async function scenarioTrueSharing(threads, iters, reps) {
  const naive = await benchVariant(
    (t, ctx) => ({ sab: ctx.c.sab, slot: 0, iterations: iters, atomic: true }),
    threads, iters, reps,
    { makeShared: () => ({ c: new NaiveSharedCounter() }),
      check: (ctx, th, it) => assertEq(ctx.c.value(), th * it, 'naive shared value') }
  );
  const sharded = await benchVariant(
    (t, ctx) => ({ sab: ctx.c.sab, slot: ctx.c.slot(t), iterations: iters }),
    threads, iters, reps,
    { makeShared: () => ({ c: new ShardedCounter(threads) }),
      check: (ctx, th, it) => assertEq(ctx.c.value(), th * it, 'sharded value') }
  );
  printComparison('true-sharing(单一热计数器)', threads, naive, sharded, 'naive-atomic(基线)', 'sharded(分片计数)');
  return { label: 'true-sharing', threads, baseline: naive, mitigated: sharded };
}

async function scenarioSparse(threads, iters, reps) {
  const writeEvery = 1000;
  const packed = await benchVariant(
    (t, ctx) => ({ sab: ctx.c.sab, slot: ctx.c.slot(t), iterations: iters, writeEvery }),
    threads, iters, reps,
    { makeShared: () => ({ c: new PackedCounters(threads) }) }
  );
  const padded = await benchVariant(
    (t, ctx) => ({ sab: ctx.c.sab, slot: ctx.c.slot(t), iterations: iters, writeEvery }),
    threads, iters, reps,
    { makeShared: () => ({ c: new PaddedCounters(threads) }) }
  );
  printComparison(`sparse(每${writeEvery}次迭代写1次)`, threads, packed, padded, 'packed(基线)', 'padded(对齐填充)');
  return { label: 'sparse', threads, baseline: packed, mitigated: padded };
}

function printComparison(label, threads, base, mit, baseName, mitName) {
  const speedup = mit.mops.mean / base.mops.mean;
  console.log(`\n### ${label}  (threads=${threads})`);
  console.log('| 方案 | 吞吐 Mops/s (mean±std) | p50 时延 ns/op | p99 时延 ns/op |');
  console.log('|---|---|---|---|');
  console.log(`| ${baseName} | ${fmt(base.mops.mean)} ± ${fmt(base.mops.std)} | ${fmt(base.p50.mean, 1)} | ${fmt(base.p99.mean, 1)} |`);
  console.log(`| ${mitName} | ${fmt(mit.mops.mean)} ± ${fmt(mit.mops.std)} | ${fmt(mit.p50.mean, 1)} | ${fmt(mit.p99.mean, 1)} |`);
  console.log(`加速比: ${fmt(speedup)}x`);
}

function assertEq(actual, expected, what) {
  if (actual !== expected) throw new Error(`${what}: expected ${expected}, got ${actual}`);
}

/** 争用画像 + 排名稳定性：同一确定性负载跑 R 轮，比较排名。 */
async function scenarioProfile(reps) {
  const V = 64, T = 8;
  const planFor = (t) => [
    [t * 8, 20000 + (t % 4) * 10000], // 每线程一个“私有”变量，频率确定但互不相同
    [5, 5000],                        // 所有线程共享的热变量（位于 line 0）
  ];
  const lineRankings = [], varRankings = [];
  let lastReport = null;
  for (let r = 0; r < reps; r++) {
    const counters = new PackedCounters(V);
    const prof = new CacheLineProfiler(V, T);
    const configs = [];
    for (let t = 0; t < T; t++) {
      configs.push({ mode: 'profiled', sab: counters.sab, profSab: prof.counts.buffer,
                     numThreads: T, threadId: t, plan: planFor(t) });
    }
    await runWorkers(configs);
    const lineRank = prof.ranking('line');
    const varRank = prof.ranking('variable');
    lineRankings.push(lineRank.map((x) => x.unit));
    varRankings.push(varRank.map((x) => x.unit));
    lastReport = { lineRank, varRank };
  }
  // 稳定性：相邻两轮 Kendall tau 与 top-3 重合度
  const taus = [], overlaps = [];
  for (let r = 1; r < reps; r++) {
    taus.push(kendallTau(lineRankings[r - 1], lineRankings[r]));
    overlaps.push(topKOverlap(varRankings[r - 1], varRankings[r], 3));
  }
  console.log(`\n### 争用画像 (variables=${V}, threads=${T}, 重复 ${reps} 轮)`);
  console.log('\n缓存行排名（按 foreignWrites = 非属主线程写入次数 降序）:');
  console.log('| rank | line | totalWrites | writers | foreignWrites |');
  console.log('|---|---|---|---|---|');
  for (const s of lastReport.lineRank) {
    console.log(`| ${s.rank} | ${s.unit} | ${s.totalWrites} | ${s.writers} | ${s.foreignWrites} |`);
  }
  console.log('\n变量排名 top-5:');
  console.log('| rank | var | totalWrites | writers | foreignWrites |');
  console.log('|---|---|---|---|---|');
  for (const s of lastReport.varRank.slice(0, 5)) {
    console.log(`| ${s.rank} | ${s.unit} | ${s.totalWrites} | ${s.writers} | ${s.foreignWrites} |`);
  }
  console.log(`\n排名稳定性: 行级 Kendall tau = [${taus.map((x) => fmt(x))}] , 变量级 top-3 重合度 = [${overlaps.map((x) => fmt(x))}]`);
  return { taus, overlaps, lineRankings, varRankings };
}

async function main() {
  const args = parseArgs();
  console.log(`node ${process.version}, cpus=${CPUS}, reps=${args.reps}, iters=${args.iters}`);

  // 1. 单线程基线（无争用，验证填充本身无负面影响）
  await scenarioFalseSharing(1, args.iters, args.reps, 'single-thread(单线程基线)');
  // 2. 线程数 = 核数：典型 false sharing
  await scenarioFalseSharing(CPUS, args.iters, args.reps, 'false-sharing(线程数=核数)');
  // 3. 线程数远大于核数（超额订阅）
  await scenarioFalseSharing(CPUS * 4, Math.floor(args.iters / 4), args.reps, 'oversubscribed(线程数=4x核数)');
  // 4. true sharing：单一热计数器，naive atomic vs 分片
  await scenarioTrueSharing(CPUS, Math.floor(args.iters / 2), args.reps);
  // 5. 稀疏写：缓解应基本无收益（也不应有明显回退）
  await scenarioSparse(CPUS, args.iters, args.reps);
  // 6. 争用画像与排名稳定性
  await scenarioProfile(Math.max(3, args.reps + 2));
}

main().catch((e) => { console.error(e); process.exit(1); });
