"""失败样本归约：把差异输入缩到最小，且保持差异结论不变。

两阶段：
  1. 结构化收缩 —— 利用 TLV 结构尝试整记录删除、NODE 清空、NODE 解包；
  2. 字节级 ddmin —— 对剩余字节做经典 delta debugging。
谓词：归约后的输入必须产生与原始输入相同的 (结论, 两边错误类别) 签名。
"""

from difftest.runner import run_one, classify

MAX_EVALS = 4000  # 谓词求值上限，防止超大样本归约过慢


class Reducer:
    def __init__(self, parse_a, parse_b):
        self.parse_a = parse_a
        self.parse_b = parse_b
        self.evals = 0

    def reduce(self, data):
        """返回 (minimized_bytes, outcome_a, outcome_b)。"""
        target = self._signature(data)
        self.evals = 0

        def pred(candidate):
            if self.evals >= MAX_EVALS:
                return False
            return self._signature(candidate) == target

        cur = self._structural(data, pred)
        cur = self._ddmin(cur, pred)
        out_a, out_b = run_one(self.parse_a, cur), run_one(self.parse_b, cur)
        return cur, out_a, out_b

    def _signature(self, data):
        self.evals += 1
        out_a, out_b = run_one(self.parse_a, data), run_one(self.parse_b, data)
        return (classify(out_a, out_b), out_a.category, out_b.category)

    @staticmethod
    def _spans(data):
        """宽松扫描顶层记录边界；遇到不完整记录即停止。"""
        spans, pos = [], 0
        while pos + 3 <= len(data):
            length = (data[pos + 1] << 8) | data[pos + 2]
            end = pos + 3 + length
            if end > len(data):
                break
            spans.append((pos, end))
            pos = end
        return spans

    def _structural(self, data, pred):
        improved = True
        while improved and self.evals < MAX_EVALS:
            improved = False
            for cand in self._structural_candidates(data):
                if len(cand) < len(data) and pred(cand):
                    data = cand
                    improved = True
                    break
        return data

    def _structural_candidates(self, data):
        for start, end in self._spans(data):
            yield data[:start] + data[end:]
            if data[start] == 0x01 and end - start > 3:
                yield data[:start] + b"\x01\x00\x00" + data[end:]
                yield data[:start] + data[start + 3:end] + data[end:]

    def _ddmin(self, data, pred):
        n = 2
        while len(data) >= 2 and self.evals < MAX_EVALS:
            chunk = (len(data) + n - 1) // n
            reduced = False
            for i in range(0, len(data), chunk):
                cand = data[:i] + data[i + chunk:]
                if len(cand) < len(data) and pred(cand):
                    data, n = cand, max(n - 1, 2)
                    reduced = True
                    break
            if not reduced:
                if n >= len(data):
                    break
                n = min(n * 2, len(data))
        i = 0
        while i < len(data) and self.evals < MAX_EVALS:
            cand = data[:i] + data[i + 1:]
            if pred(cand):
                data = cand
            else:
                i += 1
        return data
