# 协议解析器差分测试框架

纯 Python 3 标准库，无第三方依赖。用于比对"新写的解析器"与"参照实现"，
解决手写用例覆盖不到边界、差异归因不清的问题。

## 运行

```bash
# 差分测试（默认参照 parsers.reference vs 待测 parsers.candidate）
python3 run_difftest.py --cases 2000 --seed 1 --out reports

# 自测
python3 -m unittest discover -s tests -v
```

退出码：`0` 无差异，`1` 发现差异（可接 CI），`2` 用法/加载错误。

## 接入自己的实现

```bash
python3 run_difftest.py --impl-a mypkg.reference:parse --impl-b mypkg.newimpl:parse
```

被测函数签名为 `parse(data: bytes) -> 可比较的结果`，失败时抛
`difftest.errors.ParseError(category, ...)`（category 见 `difftest/errors.py`）。
未预期的异常会被归为 `UNEXPECTED:<异常名>` 类别，同样参与比对。

## 三类差异结论（另含两类一致结论）

| 结论 | 含义 |
|---|---|
| `MATCH` / `BOTH_FAIL_SAME` | 一致：结果相同，或两边同类别失败 |
| `RESULT_MISMATCH` | 两边都成功但解析结果不同 |
| `ERROR_CATEGORY_MISMATCH` | 两边都失败但错误类别不同 |
| `ONE_SIDED` | 一边成功一边失败 |

## 生成策略与覆盖率

`difftest/generator.py` 按权重生成：合法嵌套树、长度边界（0/1/255/256/65535、
UINT 0..9 字节）、截断、字节变异（字段增删、类型篡改）、纯垃圾、非法 UTF-8；
前 5 个用例固定覆盖空报文、超大报文、深度极大（达限/超限）、全部非法。
每次运行末尾打印特性覆盖率，未覆盖项附提示（如"调大 --cases"）。

## 失败归约与报告

每个差异样本先经结构化收缩（整记录删除 / NODE 清空 / NODE 解包），
再做字节级 ddmin，归约谓词保证 `(结论, 两边错误类别)` 签名不变。

输出（`--out` 目录）：

- `diff_report.json` —— 汇总计数、覆盖率、全部差异条目；
- `counterexamples/diff_XXXX.orig.bin` / `.min.bin` —— 原始与最小反例；
- `counterexamples/diff_XXXX.json` —— 最小反例 hex + 两个实现各自的输出。

## 目录

- `difftest/` —— 框架（generator / runner / reducer / coverage / report / cli）
- `parsers/reference.py` —— 演示用参照实现（TLV 协议）
- `parsers/candidate.py` —— 演示用新实现（注入 2 处差异：深度判断差一格、
  错误拒绝 8 字节 UINT），框架能稳定检出并归约
- `tests/test_difftest.py` —— 自测（分类、生成器、归约、覆盖率、端到端）
