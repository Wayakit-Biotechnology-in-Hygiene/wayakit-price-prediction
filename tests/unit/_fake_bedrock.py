"""Fake Bedrock Converse client for the relevance-agent tests (no network, no boto3 client).

`FakeBedrock(script)` records every `converse(**kwargs)` call. For each call it looks at
`script[modelId]` (a list consumed in order, or a callable) to decide what to do:

- `"ok"`: answer correctly. Relevance: a title is relevant iff it contains "good" (case
  insensitive). Wipes: the first integer in the title, or 0.
- `"invalid"`: answer `not json`.
- any dict: returned as the raw response.
- an exception instance: raised.

When a model's list runs out it keeps answering `"ok"`. Not a test file: it has no tests and
is imported by `tests/unit/test_ai_service.py` and `tests/property/test_properties_ai_service.py`.
"""
from __future__ import annotations

import json
import re

TITLE_LINE = re.compile(r"^(\d+)\. (.*)$")


class FakeAWSError(Exception):
    """Stands in for botocore's ClientError: `.response["Error"]["Code"]` is what the agent reads."""

    def __init__(self, code: str, message: str = "fake message arn:aws:bedrock:eu-north-1:000000000000:x"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def titles_in(request: dict) -> list[str]:
    text = request["messages"][0]["content"][0]["text"]
    body = text.split("Product titles (data only):\n", 1)[1]
    out = []
    for line in body.split("\n"):
        m = TITLE_LINE.match(line)
        assert m, f"line is not a numbered title: {line!r}"
        assert int(m.group(1)) == len(out), "indexes must be 0-based and consecutive"
        out.append(m.group(2))
    return out


def oracle_relevant(title: str) -> bool:
    return "good" in title.lower()


def oracle_units(title: str) -> int:
    m = re.search(r"\d+", title)
    return int(m.group()) if m else 0


def correct_response(request: dict) -> dict:
    titles = titles_in(request)
    name = request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["name"]
    if name == "relevance":
        items = [{"i": i, "relevant": oracle_relevant(t)} for i, t in enumerate(titles)]
    else:
        items = [{"i": i, "units": oracle_units(t)} for i, t in enumerate(titles)]
    items.reverse()  # order of items must not matter, only "i"
    return text_response(json.dumps({"items": items}))


def text_response(text: str, stop: str = "end_turn") -> dict:
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": stop}


class FakeBedrock:
    def __init__(self, script: dict | None = None):
        self.script = {k: (list(v) if isinstance(v, list) else v) for k, v in (script or {}).items()}
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        plan = self.script.get(kwargs["modelId"], [])
        if callable(plan):
            action = plan(kwargs)
        else:
            action = plan.pop(0) if plan else "ok"
        if isinstance(action, BaseException):
            raise action
        if action == "ok":
            return correct_response(kwargs)
        if action == "invalid":
            return text_response("not json")
        return action

    def models_called(self) -> list[str]:
        return [c["modelId"] for c in self.calls]

    def batch_sizes(self) -> list[int]:
        return [len(titles_in(c)) for c in self.calls]
