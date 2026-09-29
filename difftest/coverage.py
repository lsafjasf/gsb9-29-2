"""生成器特性覆盖率统计与提示。"""

from collections import Counter


class Coverage:
    # 特性名 -> (中文描述, 未覆盖时的提示)
    FEATURES = {
        "empty": ("空报文", "固定用例应包含空报文；请确认 --cases >= 5"),
        "oversized": ("超大报文", "固定用例应包含超大报文；请确认 --cases >= 5"),
        "deep_nesting": ("深度极大（接近/超过上限）",
                         "调大 --cases，或检查生成器 deep_chain 策略权重"),
        "all_illegal": ("全部非法序列", "固定用例应包含垃圾报文；请确认 --cases >= 5"),
        "valid": ("合法报文", "调大 --cases"),
        "boundary_length": ("长度边界（0/1/255/256/65535 等）", "调大 --cases"),
        "uint_boundary": ("UINT 长度边界（0..9 字节）", "调大 --cases"),
        "truncated": ("截断报文（字段缺失）", "调大 --cases"),
        "mutation": ("字节变异（字段增删）", "调大 --cases"),
        "unknown_type": ("未知类型字节", "调大 --cases"),
        "invalid_utf8": ("非法 UTF-8 载荷", "调大 --cases"),
    }

    def __init__(self):
        self.hits = Counter()
        self.total = 0

    def record(self, tags):
        self.total += 1
        for tag in tags:
            if tag in self.FEATURES:
                self.hits[tag] += 1

    def report(self):
        covered = [f for f in self.FEATURES if self.hits.get(f)]
        missing = [f for f in self.FEATURES if not self.hits.get(f)]
        percent = 100.0 * len(covered) / len(self.FEATURES)
        return {
            "percent": round(percent, 1),
            "covered": {f: self.hits[f] for f in covered},
            "missing": [
                {"feature": f,
                 "description": self.FEATURES[f][0],
                 "hint": self.FEATURES[f][1]}
                for f in missing
            ],
        }

    def format_text(self):
        rep = self.report()
        lines = [f"特性覆盖率: {rep['percent']}% "
                 f"({len(rep['covered'])}/{len(self.FEATURES)})"]
        for feat, desc in ((f, self.FEATURES[f][0]) for f in self.FEATURES):
            n = self.hits.get(feat, 0)
            mark = "OK " if n else "缺失"
            lines.append(f"  [{mark}] {desc}: {n} 次")
        for item in rep["missing"]:
            lines.append(f"  提示: {item['description']} 未覆盖 —— {item['hint']}")
        return "\n".join(lines)
