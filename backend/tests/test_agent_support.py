"""Shared fakes for the agent tests (no tests in this module).

`ScriptedModel` is a Strands `Model` that replays scripted assistant turns, so the real Strands
Agent loop (structured output, @tool dispatch, ConcurrentToolExecutor) runs without Bedrock.
"""
import json
from typing import Any, Callable, List, Optional

from strands.models.model import Model


def tool_use(name: str, args: dict, tool_use_id: Optional[str] = None) -> dict:
    return {"toolUse": {"name": name, "input": args, "toolUseId": tool_use_id or f"tu-{name}"}}


def text(value: str) -> dict:
    return {"text": value}


class ScriptedModel(Model):
    """Each stream() call pops the next turn: a list of blocks (tool_use(...) / text(...)),
    or a callable(messages, tool_specs) -> blocks. Records every call."""

    def __init__(self, turns: List[Any], model_id: str = "scripted"):
        self.turns = list(turns)
        self.calls: List[dict] = []
        self.config = {"model_id": model_id}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": messages, "tool_specs": tool_specs, "system_prompt": system_prompt,
                           "tool_choice": kwargs.get("tool_choice")})
        if not self.turns:
            blocks = [text("done")]
        else:
            turn = self.turns.pop(0)
            blocks = turn(messages, tool_specs) if callable(turn) else turn
        yield {"messageStart": {"role": "assistant"}}
        has_tool = False
        for block in blocks:
            if "toolUse" in block:
                has_tool = True
                tu = block["toolUse"]
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tu["toolUseId"], "name": tu["name"]}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tu["input"])}}}}
                yield {"contentBlockStop": {}}
            else:
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if has_tool else "end_turn"}}


class FailingModel(ScriptedModel):
    """A model whose every call raises (stands in for a Bedrock outage)."""

    def __init__(self, error: Exception = None, model_id: str = "failing"):
        super().__init__([], model_id=model_id)
        self.error = error or RuntimeError("Bedrock unavailable (stubbed)")

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": messages})
        raise self.error
        yield  # pragma: no cover


def decision_args(esi: int, specialty: Optional[str] = "Cardiology", summary: str = "Suspected MI.") -> dict:
    return {"esi_level": esi, "confidence_score": 0.9, "detected_language": "Urdu",
            "matched_specialty": specialty, "clinical_summary": summary, "reasoning": "Scripted."}


def classification_turn(esi: int, specialty: Optional[str] = "Cardiology") -> list:
    return [tool_use("TriageDecision", decision_args(esi, specialty))]


def all_tools_turn(specialty: str = "Cardiology") -> list:
    return [
        tool_use("alert_emergency_contact", {"emergency_details": "Your relative needs urgent help."}, "tu-call"),
        tool_use("match_specialist_doctor", {"required_specialty": specialty, "clinical_summary": "Suspected MI."}, "tu-doc"),
    ]


def model_factory(models: dict) -> Callable[[str], Model]:
    """model_id -> model instance, for TriageEngine(model_factory=...)."""
    return lambda model_id: models[model_id]
