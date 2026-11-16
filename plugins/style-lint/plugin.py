from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from common import serve


def handler(params, config):
    body = (params.get("envelope") or {}).get("body") or {}
    banned = config.get("banned", ["—", "–"])
    violations = []
    for index, choice in enumerate(body.get("choices", [])):
        if not isinstance(choice, dict):
            continue
        for representation in ("message", "delta"):
            message = choice.get(representation)
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str):
                continue
            for character in banned:
                start = 0
                while True:
                    position = content.find(character, start)
                    if position < 0:
                        break
                    prefix = content[:position].encode("utf-8")
                    violations.append(
                        {
                            "code": "style.em_dash"
                            if character == "—"
                            else "style.banned_character",
                            "severity": "error",
                            "message": f"Output contains banned character {character!r}.",
                            "hint": "Rewrite without the banned character.",
                            "retryable": True,
                            "spans": [
                                {
                                    "path": f"/choices/{index}/{representation}/content",
                                    "start": len(prefix),
                                    "end": len(prefix) + len(character.encode("utf-8")),
                                }
                            ],
                        }
                    )
                    start = position + len(character)
    return (
        {"action": "terminate", "violations": violations}
        if violations
        else {"action": "continue"}
    )


serve("acme/style-lint", {"client_bound": ["response"]}, handler, read_only=True)
