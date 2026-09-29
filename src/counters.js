'use strict';
/**
 * 三种计数器布局 + 两种缓解实现。
 *
 *  - PackedCounters   基线：n 个计数器紧密排列，多个计数器落在同一缓存行
 *                     → 不同线程写“不同变量”也会互相踢行（false sharing）。
 *  - PaddedCounters   缓解一（对齐填充）：每个计数器独占 64B 缓存行。
 *  - NaiveSharedCounter 基线：所有线程 Atomics.add 同一个地址（true sharing）。
 *  - ShardedCounter   缓解二（分片计数）：每线程一个对齐分片，读时求和，
 *                     把“写一个共享变量”变成“写各自的私有行”。
 */
const { SLOTS_PER_LINE } = require('./profiler.js');

class PackedCounters {
  constructor(n) {
    this.n = n;
    this.sab = new SharedArrayBuffer(n * 4);
    this.view = new Int32Array(this.sab);
  }
  slot(i) { return i; }                 // 相邻计数器共享缓存行
  value(i) { return this.view[i]; }
}

class PaddedCounters {
  constructor(n) {
    this.n = n;
    // 多分配一行，SAB 在 V8 中页对齐，起点即缓存行对齐
    this.sab = new SharedArrayBuffer((n + 1) * SLOTS_PER_LINE * 4);
    this.view = new Int32Array(this.sab);
  }
  slot(i) { return i * SLOTS_PER_LINE; } // 每个计数器独占一个 64B 行
  value(i) { return this.view[i * SLOTS_PER_LINE]; }
}

class NaiveSharedCounter {
  constructor() {
    this.sab = new SharedArrayBuffer(4);
    this.view = new Int32Array(this.sab);
  }
  slot() { return 0; }
  value() { return Atomics.load(this.view, 0); }
}

class ShardedCounter {
  constructor(numThreads) {
    this.numThreads = numThreads;
    this.sab = new SharedArrayBuffer((numThreads + 1) * SLOTS_PER_LINE * 4);
    this.view = new Int32Array(this.sab);
  }
  slot(threadId) { return threadId * SLOTS_PER_LINE; }
  value() {
    let sum = 0;
    for (let t = 0; t < this.numThreads; t++) sum += this.view[t * SLOTS_PER_LINE];
    return sum;
  }
}

module.exports = { PackedCounters, PaddedCounters, NaiveSharedCounter, ShardedCounter };
