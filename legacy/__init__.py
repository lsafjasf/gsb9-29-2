"""重构前各模块的原始抽样实现（冻结保留，仅用于差分测试基准）。

问题回顾：
- bi_report   ：直接用全局 random，结果不可复现；k > n 时抛底层 ValueError。
- etl_export  ：每次调用内部新建 random.Random()，默认不可复现；
                负权重被静默吞掉、全零权重时以 IndexError 崩溃。
- training_data：参数语义不同（frac 为比例而非条数）；
                随机源是裸的 rand_fn，索引用 int(rand_fn()*n) 自行推导。
三套实现对同一批数据给出互相矛盾的结果。
"""
