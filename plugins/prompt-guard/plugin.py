from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from common import serve


def handler(params, config):
    body = (params.get("envelope") or {}).get("body") or {}
    patterns = config.get("patterns", [r".*ignore previous instructions.*"])
    for message in body.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                start = len(content[: match.start()].encode("utf-8"))
                end = len(content[: match.end()].encode("utf-8"))
                return {
                    "action": "terminate",
                    "violations": [
                        {
                            "code": "security.prompt_injection",
                            "severity": "critical",
                            "message": "Prompt contains a suspected instruction injection.",
                            "hint": "Remove the injected instruction and try again.",
                            "retryable": False,
                            "spans": [
                                {
                                    "path": f"/messages/{body['messages'].index(message)}/content",
                                    "start": start,
                                    "end": end,
                                }
                            ],
                        }
                    ],
                }
    return {"action": "continue"}


serve("acme/prompt-guard", {"server_bound": ["request"]}, handler)
