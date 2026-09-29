"""联合准入控制（joint admission control）库。

公共 API：
    Vector                  资源向量
    Rejected                申请被拒绝（超量/超时）异常
    AdmissionController     整体向量、原子准入、可排队可预留
    PerResourceAdmission    逐资源判定的对照实现
"""

from .admission import (
    AdmissionController,
    Grant,
    Rejected,
    Vector,
)

__all__ = [
    "Vector",
    "Rejected",
    "AdmissionController",
    "Grant",
]
