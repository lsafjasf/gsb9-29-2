"""组合索引库：键等值/范围 + 空间矩形/kNN，纯标准库。"""

from .keyindex import KeyIndex
from .rtree import QueryStats, RTree
from .store import IndexedStore

__all__ = ["IndexedStore", "KeyIndex", "RTree", "QueryStats"]
