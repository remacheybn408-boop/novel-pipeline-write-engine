from __future__ import annotations

import hashlib
import re
from collections import OrderedDict


def normalized_hash(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def deduplicate_blocks(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge identical text while retaining every source reference."""
    merged: OrderedDict[str, dict[str, object]] = OrderedDict()
    for block in blocks:
        content = str(block.get("content", ""))
        key = normalized_hash(content)
        source_ids = [str(item) for item in block.get("source_ids", [block.get("id", "")]) if item]
        if key not in merged:
            merged[key] = {**block, "source_ids": source_ids}
            continue
        previous = merged[key]
        previous["source_ids"] = list(dict.fromkeys([*previous.get("source_ids", []), *source_ids]))
        previous["pinned"] = bool(previous.get("pinned")) or bool(block.get("pinned"))
        previous["priority"] = min(int(previous.get("priority", 100)), int(block.get("priority", 100)))
    return list(merged.values())
