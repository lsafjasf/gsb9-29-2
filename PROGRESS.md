# 进度与剩余工作

## 已完成（冒烟测试通过）
- `joint_admission/admission.py` 核心库：
  - `Vector` 整体资源向量（逐维比较/加减，不可变）
  - `AdmissionController.acquire(req, timeout)`：原子整体准入；
    超总容量立即拒（over_capacity）；timeout=0 非阻塞；支持排队与超时
  - FIFO 队列 + 老化屏障（starvation_threshold）有界插队防饥饿
  - 部分/整体释放（Grant.release(partial)），释放即唤醒排队者
  - 预留语义：申请到即预留全程峰值向量（reserve=acquire）
  - stats()：各维/平均利用率、拒绝率（分类）、排队时长
    mean/std/p50/p95/max、平均持有时间
  - 线程安全（threading.Lock + Event 唤醒）
- `joint_admission/__init__.py`（per_resource 导入已临时移除）

## 剩余工作
1. `joint_admission/per_resource.py`：逐资源判定对照实现，
   构造"逐维都通过、联合超限"反例
2. `tests/test_admission.py`（unittest，仅标准库）：
   - 单资源吃紧 / 全资源吃紧 / 需求超总量 / 中途释放
   - 联合拒绝反例（对比逐资源方案）
   - 无死锁测试（多线程混合申请释放，全部终止 + 一致性）
   - 防饥饿测试（老化屏障后等待时长上界）
   - 超时拒绝、幂等释放、维度校验等边界
3. `simulate.py`：离散事件仿真，输出利用率/排队时长/拒绝率
   对比表（联合 vs 逐资源），结果落 results/
4. `README.md`：无死锁论证（Coffman 四条件：禁止 hold-and-wait
   与 circular wait）、防饥饿论证、边界用例、运行方式
   python -m unittest / python simulate.py
