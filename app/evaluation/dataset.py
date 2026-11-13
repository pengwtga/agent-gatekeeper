"""数据集加载与统计"""

import json
import random
from pathlib import Path
from typing import Any, Optional

DATA_FILE = Path(__file__).parent.parent.parent / "data" / "ssrm" / "incidents.jsonl"  # app/evaluation/ → 项目根/data/ssrm


def load_dataset(limit: Optional[int] = None, shuffle: bool = False,
                 path: Optional[Path] = None) -> list[dict[str, Any]]:
    rows = []
    with (path or DATA_FILE).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if shuffle:
        random.Random(42).shuffle(rows)
    if limit:
        rows = rows[:limit]
    return rows


def stats() -> dict[str, Any]:
    rows = load_dataset()
    from collections import Counter
    c = Counter(r["label"] for r in rows)
    return {"total": len(rows), "by_label": dict(c)}
