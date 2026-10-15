from collections import Counter
from typing import Dict, Optional, Set, Tuple


class UniqueTrackCounter:
    """Count each tracked class/ID combination once."""

    def __init__(self) -> None:
        self._seen: Set[Tuple[int, str]] = set()
        self._counts_by_class: Counter[str] = Counter()

    def register(self, track_id: Optional[int], class_name: str) -> bool:
        if track_id is None:
            return False

        key = (int(track_id), class_name)
        if key in self._seen:
            return False

        self._seen.add(key)
        self._counts_by_class[class_name] += 1
        return True

    def to_summary(self) -> Dict[str, object]:
        return {
            "unique_detections": len(self._seen),
            "unique_detections_by_class": dict(self._counts_by_class),
        }
