'use strict';
/**
 * CacheLineProfiler — 按共享单元（缓存行 / 变量）统计多线程写入。
 *
 * 模型约定（统计口径）：
 *  - 缓存行 = 64 字节 = 16 个 int32 槽位；变量 v 属于行 floor(v / 16)。
 *  - 每次写由被测代码显式上报 recordWrite(varId, threadId)，
 *    记录到独立的 SharedArrayBuffer（不影响被测缓冲区的布局）。
 *  - 争用指标（对任意单元 u，u 可以是变量或缓存行）：
 *      totalWrites(u)   = 所有线程对 u 的写入总数
 *      writers(u)       = 写过 u 的不同线程数
 *      maxThreadWrites  = 单线程最大写入数（“属主”线程的写入量）
 *      foreignWrites(u) = totalWrites - maxThreadWrites
 *        —— 非属主线程的写入次数。每次这样的写在 MESI 协议下都会
 *           引起一次缓存行所有权转移（ping-pong），是争用的直接代价，
 *           因此用它作为争用评分 contentionScore。
 *  - 排名：按 contentionScore 降序；同分按 totalWrites 降序。
 */
const CACHE_LINE_BYTES = 64;
const SLOTS_PER_LINE = CACHE_LINE_BYTES / Int32Array.BYTES_PER_ELEMENT; // 16

class CacheLineProfiler {
  constructor(numVars, numThreads) {
    if (!Number.isInteger(numVars) || numVars < 1) throw new RangeError('numVars must be >= 1');
    if (!Number.isInteger(numThreads) || numThreads < 1) throw new RangeError('numThreads must be >= 1');
    this.numVars = numVars;
    this.numThreads = numThreads;
    this.counts = new Int32Array(new SharedArrayBuffer(numVars * numThreads * 4));
  }

  static lineOf(varId) { return Math.floor(varId / SLOTS_PER_LINE); }
  lineOf(varId) { return CacheLineProfiler.lineOf(varId); }

  recordWrite(varId, threadId) {
    Atomics.add(this.counts, varId * this.numThreads + threadId, 1);
  }

  /** 把 [var][thread] 原始矩阵先聚合到 [unit][thread]，再计算每个单元的统计。 */
  _unitStats(unitOf, numUnits) {
    const perThread = [];
    for (let u = 0; u < numUnits; u++) perThread.push(new Array(this.numThreads).fill(0));
    for (let v = 0; v < this.numVars; v++) {
      const u = unitOf(v);
      for (let t = 0; t < this.numThreads; t++) {
        perThread[u][t] += this.counts[v * this.numThreads + t];
      }
    }
    return perThread.map((row, u) => {
      let totalWrites = 0, writers = 0, maxThreadWrites = 0;
      for (const c of row) {
        if (c === 0) continue;
        totalWrites += c;
        writers += 1;
        if (c > maxThreadWrites) maxThreadWrites = c;
      }
      return { unit: u, totalWrites, writers, maxThreadWrites,
               foreignWrites: totalWrites - maxThreadWrites };
    });
  }

  /** 按变量的统计（每个变量即一个单元）。 */
  perVariable() { return this._unitStats((v) => v, this.numVars); }

  /** 按缓存行的统计（同一行内所有变量聚合）。 */
  perLine() {
    const numLines = Math.ceil(this.numVars / SLOTS_PER_LINE);
    return this._unitStats((v) => this.lineOf(v), numLines);
  }

  /** 争用排名：contentionScore(=foreignWrites) 降序，totalWrites 次之。 */
  ranking(granularity = 'line') {
    const stats = granularity === 'line' ? this.perLine() : this.perVariable();
    return stats
      .filter((s) => s.totalWrites > 0)
      .sort((a, b) => b.foreignWrites - a.foreignWrites || b.totalWrites - a.totalWrites)
      .map((s, i) => ({ rank: i + 1, granularity, ...s }));
  }

  reset() { this.counts.fill(0); }
}

/** Kendall tau-b 等级相关（用于跨运行排名稳定性）。输入为单元 id 的排名数组。 */
function kendallTau(rankA, rankB) {
  const posB = new Map(rankB.map((id, i) => [id, i]));
  const a = rankA.filter((id) => posB.has(id));
  let concordant = 0, discordant = 0;
  for (let i = 0; i < a.length; i++) {
    for (let j = i + 1; j < a.length; j++) {
      if (posB.get(a[i]) < posB.get(a[j])) concordant++;
      else discordant++;
    }
  }
  const n = concordant + discordant;
  return n === 0 ? 1 : (concordant - discordant) / n;
}

/** 两个排名（单元 id 数组）前 k 名的交集比例。 */
function topKOverlap(rankA, rankB, k) {
  const ka = new Set(rankA.slice(0, k));
  const kb = new Set(rankB.slice(0, k));
  if (ka.size === 0 && kb.size === 0) return 1;
  let hit = 0;
  for (const id of ka) if (kb.has(id)) hit++;
  return hit / Math.max(ka.size, kb.size);
}

module.exports = { CacheLineProfiler, CACHE_LINE_BYTES, SLOTS_PER_LINE, kendallTau, topKOverlap };
