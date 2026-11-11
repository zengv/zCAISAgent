"""MCP stdio 测试桩：换行分隔 JSON-RPC 2.0，提供 echo / add 两个工具。"""

from __future__ import annotations

import json
import sys
import time

_TOOLS = [
    {
        "name": "echo",
        "description": "回显文本，并按需延迟响应以测试超时",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "delay": {"type": "number"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add",
        "description": "两数相加",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
]


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _call(name: str, args: dict) -> dict:
    if name == "echo":
        delay = float(args.get("delay") or 0)
        if delay:
            time.sleep(delay)
        text = str(args.get("text") or "")
        if text == "__error__":
            return {"content": [{"type": "text", "text": "boom"}], "isError": True}
        return {
            "content": [
                {"type": "text", "text": text},
                {"type": "text", "text": "done"},
            ],
            "isError": False,
        }
    if name == "add":
        total = float(args.get("a", 0)) + float(args.get("b", 0))
        return {"content": [{"type": "text", "text": str(total)}], "isError": False}
    return {
        "content": [{"type": "text", "text": f"unknown tool: {name}"}],
        "isError": True,
    }


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo-server", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": _TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = _call(
                str(params.get("name") or ""),
                dict(params.get("arguments") or {}),
            )
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )
            continue
        _send({"jsonrpc": "2.0", "id": message["id"], "result": result})


if __name__ == "__main__":
    main()
