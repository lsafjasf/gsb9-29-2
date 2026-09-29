"""争用分析：把 MemoryModel 的行级统计汇总成可读报告。

判定口径：
- shared       : 写该行的不同线程数 >= 2
- false-sharing: shared 且该行覆盖 >= 2 个变量（不同变量挤在同一行被多线程写）
- true-sharing : shared 且该行只有 1 个变量（多线程写同一变量）
- private      : 只有一个线程写，无争用
- 争用排名依据：transfers（行所有权跨核易手次数），并列时按 cycles、writes 排序。
  transfers 与"伪共享/真共享导致的 ping-pong"直接对应，是最稳定的排名信号。
"""
from dataclasses import dataclass
from memory_model import LINE_SIZE


@dataclass
class LineReport:
    line: int
    symbols: list
    writes: int
    n_writers: int
    transfers: int
    cycles: int
    kind: str  # "false-sharing" | "true-sharing" | "private"

    @property
    def transfer_ratio(self):
        return self.transfers / self.writes if self.writes else 0.0


def analyze(mem):
    reports = []
    for line, st in mem.lines.items():
        syms = mem.symbols_for_line(line)
        shared = len(st.writers) >= 2
        if shared and len(syms) >= 2:
            kind = "false-sharing"
        elif shared:
            kind = "true-sharing"
        else:
            kind = "private"
        reports.append(LineReport(
            line=line, symbols=syms, writes=st.writes,
            n_writers=len(st.writers), transfers=st.transfers,
            cycles=st.cycles, kind=kind))
    reports.sort(key=lambda r: (r.transfers, r.cycles, r.writes), reverse=True)
    return reports


def ranking_signature(reports):
    """用于跨运行比较排名稳定性的签名：行号+排名的有序元组。"""
    return tuple(r.line for r in reports)


def format_report(reports, top=None):
    rows = []
    header = (f"{'排名':<4}{'缓存行':<10}{'变量':<28}{'写次数':>9}"
              f"{'写线程数':>9}{'行转移':>9}{'周期数':>12}  类型")
    rows.append(header)
    rows.append("-" * len(header.expandtabs()))
    for i, r in enumerate(reports[:top] if top else reports, 1):
        syms = ",".join(r.symbols)
        if len(syms) > 26:
            syms = syms[:23] + "..."
        rows.append(f"{i:<4}{'0x'+format(r.line * LINE_SIZE, '06x'):<10}"
                    f"{syms:<28}{r.writes:>9}{r.n_writers:>9}"
                    f"{r.transfers:>9}{r.cycles:>12}  {r.kind}")
    contended = [r for r in reports if r.kind != "private"]
    rows.append(f"\n争用行数: {len(contended)} / 总行数: {len(reports)}")
    return "\n".join(rows)
