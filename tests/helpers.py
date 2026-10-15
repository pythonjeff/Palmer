"""Fakes shared across test files. Import what you need:

    from tests.helpers import llm_reply, Block, Resp, tool_by_name, drive_tool
"""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from palmer.tools_def import TOOLS


def llm_reply(text: str) -> MagicMock:
    """A messages.create response whose one content block carries `text`."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class Block:
    """A content block as the Anthropic SDK shapes it: attribute access only."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    """A messages.create response with real `content` and `stop_reason`."""
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


def tool_by_name(name: str) -> dict | None:
    return next((t for t in TOOLS if t["name"] == name), None)


def drive_tool(tool_name, tool_input, *, message="hi", reply="done", profile=None,
               history=(), patches=()):
    """Run agent.get_reply through exactly one tool call.

    The model is faked to call `tool_name` with `tool_input`, then answer
    `reply`. Returns (text, tool_result, mocks): the reply text, the tool_result
    string the model was handed back, and the entered values of `patches` in
    order — pass `patch.object(...)` objects and read the mocks back here."""
    from palmer import agent
    calls = []
    responses = [
        Resp([Block(type="tool_use", name=tool_name, id="t1", input=tool_input)], "tool_use"),
        Resp([Block(type="text", text=reply)], "end_turn"),
    ]

    def _create(**kw):
        calls.append(kw)
        return responses[len(calls) - 1]

    with ExitStack() as stack:
        stack.enter_context(patch.object(agent, "_build_system", return_value="sys"))
        stack.enter_context(patch.object(agent, "get_history", return_value=list(history)))
        stack.enter_context(patch.object(agent, "get_profile",
                                         return_value=profile if profile is not None else {}))
        mocks = [stack.enter_context(p) for p in patches]
        stack.enter_context(patch.object(agent.client.messages, "create", side_effect=_create))
        text, _gif = agent.get_reply("+1555", message)
    result = calls[1]["messages"][-1]["content"][0]["content"]
    return text, result, mocks
