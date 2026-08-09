"""Tolerant parsing of model JSON output (agents layer shared helper).

Models frequently wrap the requested JSON object in a markdown code fence or
prepend a sentence of prose despite the output contract. parse_model_json
follows the same defensive pattern as application/work/summarize_chapter.py
(_strip_code_fence + JSONDecoder().raw_decode from the first "{"): strip the
fence, try a plain json.loads, then raw_decode from the first "{". Only when
both fail is the original JSONDecodeError re-raised, so the executor keeps
its malformed_json retry semantics for genuinely broken output.
"""

from __future__ import annotations

import json
import re
from typing import Any

_CODE_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def parse_model_json(text: str) -> Any:
    """Parse a single JSON value from raw model output.

    Tolerates a surrounding markdown code fence and leading prose before the
    first "{". Raises json.JSONDecodeError when no parseable JSON remains.

    strict=False: long-form novel text frequently contains literal control
    characters (raw newlines, form feeds) inside JSON string values, which
    strict JSON rejects — accepting them avoids spurious malformed_json
    retries that otherwise burn whole task attempts.
    """
    stripped = text.strip()
    match = _CODE_FENCE_PATTERN.match(stripped)
    candidate = match.group("body") if match else stripped
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        start = candidate.find("{")
        if start == -1:
            raise
        return json.JSONDecoder(strict=False).raw_decode(candidate[start:])[0]
