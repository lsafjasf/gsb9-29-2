"""logrouter: 按规则路由日志到多目的地，目的地间故障隔离。"""
from .rules import Rule, FieldEquals, FieldPrefix, FieldRegex
from .router import Router
from .destination import Destination, DestinationWorker

__all__ = [
    "Rule",
    "FieldEquals",
    "FieldPrefix",
    "FieldRegex",
    "Router",
    "Destination",
    "DestinationWorker",
]
