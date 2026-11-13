"""Robust JSON extraction/validation for LLM output."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def extract_json(text: str) -> Any:
    """Return the first JSON object/array found in ``text`` (handles ``` fences, prose, trailing commas)."""
    if text is None:
        raise ValueError("empty model output")
    candidates: list[str] = []
    fence = _FENCE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text.strip())
    decoder = json.JSONDecoder()
    for cand in candidates:
        for attempt in (cand, _TRAILING_COMMA.sub(r"\1", cand)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass
            for i, ch in enumerate(attempt):
                if ch in "{[":
                    try:
                        obj, _ = decoder.raw_decode(attempt[i:])
                        return obj
                    except json.JSONDecodeError:
                        continue
    raise ValueError("no JSON object found in model output")


def parse_model(schema: type[T], text: str) -> T:
    data = extract_json(text)
    if isinstance(data, list) and "items" in schema.model_fields:
        data = {"items": data}
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"model output did not match schema: {exc.errors()[:3]}") from exc


def schema_hint(schema: type[BaseModel]) -> str:
    """Compact JSON schema for prompts."""
    return json.dumps(schema.model_json_schema(), separators=(",", ":"))[:6000]


def strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic schema into the strict form required by structured-output APIs
    (every object: additionalProperties=false and all properties required; $defs inlined)."""
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(dict(defs.get(name, {})))
            node = {k: resolve(v) for k, v in node.items() if k not in ("title", "default", "examples")}
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties", {})
                node["type"] = "object"
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            return node
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(raw)
