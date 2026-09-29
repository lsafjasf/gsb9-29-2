'use strict';
/**
 * 基准工作线程。通过 workerData 接收任务，postMessage 回传：
 *   { ops, nanos, batches: number[] }  batches 为每批耗时的纳秒数（用于时延分位数）
 */
const { parentPort, workerData } = require('worker_threads');
const { performance } = require('perf_hooks');

const BATCH = 4096;

function runCounter(wd) {
  const view = new Int32Array(wd.sab);
  const slot = wd.slot;
  const iters = wd.iterations;
  const writeEvery = wd.writeEvery || 1; // >1 表示稀疏写：其余迭代做本地计算
  const batches = [];
  let sink = 0;
  const t0 = performance.now();
  let bt0 = t0;
  if (wd.atomic) {
    for (let i = 0; i < iters; i++) {
      Atomics.add(view, slot, 1);
      if ((i & (BATCH - 1)) === BATCH - 1) {
        const now = performance.now();
        batches.push((now - bt0) * 1e6);
        bt0 = now;
      }
    }
  } else if (writeEvery === 1) {
    for (let i = 0; i < iters; i++) {
      view[slot]++; // 每次迭代都真实写共享内存（false sharing 场景）
      if ((i & (BATCH - 1)) === BATCH - 1) {
        const now = performance.now();
        batches.push((now - bt0) * 1e6);
        bt0 = now;
      }
    }
  } else {
    for (let i = 0; i < iters; i++) {
      if (i % writeEvery === 0) view[slot]++;
      else sink ^= i; // 稀疏写时的本地占位计算，防止空循环被优化
      if ((i & (BATCH - 1)) === BATCH - 1) {
        const now = performance.now();
        batches.push((now - bt0) * 1e6);
        bt0 = now;
      }
    }
  }
  const nanos = (performance.now() - t0) * 1e6;
  return { ops: iters, nanos, batches, sink };
}

/** 画像模式：按确定性模式写入，同时向 profiler 上报，用于争用排名稳定性。 */
function runProfiled(wd) {
  const view = new Int32Array(wd.sab);
  const prof = new Int32Array(wd.profSab);
  const numThreads = wd.numThreads;
  const plan = wd.plan; // Array<[varId, count]>
  const t0 = performance.now();
  for (const [varId, count] of plan) {
    for (let i = 0; i < count; i++) {
      Atomics.add(view, varId, 1); // 画像模式不计时，用原子写保证计数器本身也可校验
      Atomics.add(prof, varId * numThreads + wd.threadId, 1);
    }
  }
  const nanos = (performance.now() - t0) * 1e6;
  const ops = plan.reduce((s, p) => s + p[1], 0);
  return { ops, nanos, batches: [] };
}

const result = workerData.mode === 'profiled' ? runProfiled(workerData) : runCounter(workerData);
parentPort.postMessage(result);
