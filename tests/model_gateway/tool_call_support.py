"""Fixed synthetic response from the approved TOOL_CALL specification, never a runtime source."""

from typing import Any


def tool_response() -> dict[str, Any]:
    return {
        "id": "chatcmpl-synthetic-tool",
        "object": "chat.completion",
        "model": "synthetic-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_synthetic",
                            "type": "function",
                            "function": {"name": "platform_probe", "arguments": '{"marker":"ok"}'},
                        }
                    ],
                },
            }
        ],
    }
