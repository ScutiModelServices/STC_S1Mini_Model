"""The answer cache shared by DSC_Laya_Model, CNV_MiniCPM52B_Model and CNV_BitNet2B_Model (the same file in all three)."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class AnswerCache:
    """The last answers, least recently used out. Per replica and in memory, so with N replicas the hit rate is
    about 1/N.

    shortcut: no shared store between replicas, upgrade only if the hit rate matters."""

    def __init__(self) -> None:
        self._items: OrderedDict[Any, Any] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Any, size: int) -> Any | None:
        """The kept answer for `key`, or None. `size` 0 turns the cache off and empties it."""
        if not size:
            self._items.clear()
            return None
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, key: Any, item: Any, size: int) -> None:
        if not size:
            return
        self._items[key] = item
        self._items.move_to_end(key)
        while len(self._items) > size:
            self._items.popitem(last=False)
