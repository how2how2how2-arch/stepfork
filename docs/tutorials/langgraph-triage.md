# Tutorial: a LangGraph triage agent, from a recorded run to a regression test

This is a step-by-step walkthrough rather than an API reference: you build one
small LangGraph agent, record a run of it, replay that run without calling a
model or a tool, and turn it into a pytest test that fails on the buggy agent
and passes on the fixed one.

Everything here runs offline. A deterministic scripted chat model stands in for
a real provider, so there is no API key and no network call. For the adapter's
options and its exact semantics see [the LangGraph guide](../langgraph.md); for
the larger worked example that also diffs and validates two bundles see
[`examples/langgraph_agent`](https://github.com/utsab345/stepfork/tree/main/examples/langgraph_agent).
This page does not repeat either of them — it walks the steps in order.

Requirements: Python 3.11, 3.12 or 3.13, and the `langgraph` extra, which is
what installs the adapter (the core package does not depend on LangChain or
LangGraph):

```bash
pip install "stepfork[langgraph]"
```

Working from a checkout instead, `uv sync --extra langgraph` installs the same
thing. The commands below call `python` and `stepfork`; run them in the
directory where you save the agent.

## Step 1 — Write the agent

The agent looks a ticket up through an instrumented tool and decides whether to
escalate it. The decision is deliberately wrong: it escalates tickets with
priority `low` and queues `high` ones, which is the bug you will pin down.

Save this as `triage_agent.py`:

<!-- langgraph-tutorial:start -->
```python
from __future__ import annotations

from typing import Annotated, Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import Field
from typing_extensions import TypedDict

from stepfork import record
from stepfork.integrations.langgraph import traced_chat_model, traced_tool

LOOKUPS: list[str] = []


@traced_tool
def lookup_ticket(ticket_id: str) -> str:
    """Look up a support ticket by id."""
    LOOKUPS.append(ticket_id)
    return f"ticket={ticket_id};priority=high;plan=enterprise"


SCRIPT = [
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup_ticket",
                "args": {"ticket_id": "TCK-42"},
                "id": "call_lookup",
            }
        ],
    ),
    AIMessage(content="Triage complete."),
]


class ScriptedChatModel(BaseChatModel):
    """Deterministic stand-in for a chat model; no API key required."""

    model_name: str = "scripted-triage"
    script: list[AIMessage] = Field(default_factory=lambda: list(SCRIPT))
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


class TriageState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    answer: str


def build_graph(decide: Any) -> Any:
    model = traced_chat_model(ScriptedChatModel())
    tools = [lookup_ticket]
    model_with_tools = model.bind_tools(tools)

    def agent(state: TriageState) -> dict[str, Any]:
        return {"messages": [model_with_tools.invoke(state["messages"])]}

    def route(state: TriageState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else "finish"

    def finish(state: TriageState) -> dict[str, Any]:
        result = next(
            m.content for m in state["messages"] if isinstance(m, ToolMessage)
        )
        return {"answer": decide(result)}

    graph = StateGraph(TriageState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("finish", finish)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", "finish": "finish"})
    graph.add_edge("tools", "agent")
    graph.add_edge("finish", END)
    return graph.compile()


def decide_buggy(lookup: str) -> str:
    priority = lookup.split("priority=")[1].split(";")[0]
    return "escalate" if priority == "low" else "queue"


def run_agent() -> dict[str, Any]:
    result = build_graph(decide_buggy).invoke(
        {"messages": [HumanMessage(content="Triage ticket TCK-42")]}
    )
    return {"decision": result["answer"]}


if __name__ == "__main__":
    with record("triage-agent", output="triage.sftrace") as session:
        outcome = run_agent()
        session.set_output(outcome)
    print(outcome)
```
<!-- langgraph-tutorial:end -->

Two things carry the tracing. `traced_chat_model(...)` wraps the model instance
in place, and `@traced_tool` wraps the tool function, so the graph itself —
compilation, edges, routing, node bodies — is ordinary LangGraph.

## Step 2 — Record a run

```bash
python triage_agent.py
```

```text
{'decision': 'queue'}
```

The run wrote a `.sftrace` bundle next to the script:

```text
triage.sftrace/
├── events.jsonl
├── integrity.json
├── manifest.json
└── redactions.json
```

`events.jsonl` is the part worth looking at. It holds one line per event, and
this run produced eight, in this order:

```text
run_start → llm_request → llm_response → tool_call → tool_result
          → llm_request → llm_response → run_end
```

The model boundary and the tool boundary each appear as a pair. Those pairs are
what replay substitutes later.

## Step 3 — Check the bundle

Before trusting a trace, validate it. `--verify-integrity` also re-hashes every
payload against the digests recorded in `integrity.json`:

```bash
stepfork validate triage.sftrace --verify-integrity
```

```text
Stepfork Trace Validation

Trace    triage.sftrace

Schema    0.1
Events    8
Mode      strict

Structure: PASS
Integrity: VERIFIED

manifest.json      verified
events.jsonl       verified
redactions.json    verified

✓ Manifest valid
✓ Event structure valid
✓ Event IDs unique
✓ Parent references valid
✓ Run IDs consistent
✓ Event count matches

Validation passed.
```

The summary is the quickest way to see what a bundle actually captured:

```bash
stepfork inspect triage.sftrace
```

```text
Agent:         triage-agent
Status:        COMPLETED
Events:        8
Schema:        0.1
LLM calls:     2
Tool calls:    1
Integrity:     VERIFIED
```

`LLM calls: 2` and `Tool calls: 1` are counted from the events, not from the
agent, which makes this the first place a gap shows up: a boundary you expected
to be recorded and is not has no event here, and `stepfork inspect --events`
shows you the timeline it came from.

## Step 4 — Replay the run

`replay` runs a local entrypoint of yours under the recorded trace. Only the
function you name is imported and executed; the bundle is data, and Stepfork
never runs code out of it.

```bash
stepfork replay triage.sftrace --entrypoint triage_agent:run_agent
```

```text
Dependency Calls
  1. llm scripted/scripted-triage (substituted)
  2. tool lookup_ticket (substituted)
  3. llm scripted/scripted-triage (substituted)
Dependency matching: COMPLETE

Execution: COMPLETED
Recorded behavior: MATCHED
Status: COMPLETED
Final result: {"decision":"queue"}
```

`(substituted)` is the whole point: the agent's own graph ran again, but each
model call and the tool call were answered from the recording. The tool body
never executed, so `LOOKUPS` stays empty — you can assert that yourself after a
replay with `ReplaySession`, which is what
[the LangGraph guide](../langgraph.md) shows.

`Recorded behavior: MATCHED` says this replay agreed with the recording, which
is expected: nothing has changed yet.

## Step 5 — Write down what should have happened

Replay tells you what the agent did. The regression test needs what it *should*
do, which is a separate file you write by hand:

```bash
printf '{"decision": "escalate"}' > expected.json
```

An enterprise ticket at priority `high` should be escalated. That one line is
the whole expectation; the exported test compares the entrypoint's return value
against it.

## Step 6 — Export the test, and watch it fail

Export a test that runs the buggy entrypoint against that expectation:

```bash
stepfork export triage.sftrace --pytest \
  --entrypoint triage_agent:run_agent \
  --expect-output expected.json \
  --output test_triage.py --overwrite
```

```text
Wrote test_triage.py
Entrypoint: triage_agent:run_agent
Mode:       frozen
Expectation: {"decision": "escalate"}
```

Then run it:

```bash
pytest test_triage.py
```

```text
E       AssertionError: behavior mismatch at 'decision':
        expected "escalate", got "queue"

FAILED test_triage.py::test_stepfork_regression_triage
1 failed in 0.35s
```

The failure is the product: the bug is now a test, and the message names the
field that diverged and both values. The test ran in frozen mode, so it needed
no model and no network to reach that verdict.

## Step 7 — Fix the bug and watch it pass

Correct the comparison — `high` should escalate, not `low` — and add the
corrected decision and its entrypoint to `triage_agent.py`:

<!-- langgraph-tutorial-fix:start -->
```python
def decide_fixed(lookup: str) -> str:
    priority = lookup.split("priority=")[1].split(";")[0]
    return "escalate" if priority == "high" else "queue"


def run_agent_fixed() -> dict[str, Any]:
    result = build_graph(decide_fixed).invoke(
        {"messages": [HumanMessage(content="Triage ticket TCK-42")]}
    )
    return {"decision": result["answer"]}
```
<!-- langgraph-tutorial-fix:end -->

The trace stays as it is: exporting again does not re-record, and both
entrypoints are run against that one recording. Export the same expectation
against the fixed entrypoint:

```bash
stepfork export triage.sftrace --pytest \
  --entrypoint triage_agent:run_agent_fixed \
  --expect-output expected.json \
  --output test_triage_fixed.py --overwrite

pytest test_triage_fixed.py
```

```text
1 passed in 0.32s
```

Two tests, one expectation, opposite verdicts:

```bash
pytest test_triage.py test_triage_fixed.py
```

```text
FAILED test_triage.py::test_stepfork_regression_triage
1 failed, 1 passed in 0.35s
```

Commit `test_triage.py` alongside `triage.sftrace` and the bug cannot come back
silently: the test fails in CI the moment the decision changes, without a
provider, a key, or a recorded re-run.

## What this does and does not prove

Recorded by the adapter: chat-model generation, the traced tool's execution,
the agent's final output, and exceptions raised inside those boundaries.

Not recorded, and so not protected by the exported test: retrieval and
vector-store calls, checkpointers, streaming responses, and any dependency you
did not wrap. A recorded run is also only the path it took — replay surfaces a
divergence at the first boundary whose input stops matching, and it does not
tell you about a branch the recording never visited. The scripted model here is
deterministic on purpose: this tutorial shows the record/replay/regress
mechanism, and says nothing about how a real model would behave.

## Where to go next

- [The LangGraph guide](../langgraph.md) — adapter semantics, what is captured,
  and the troubleshooting list.
- [`examples/langgraph_agent`](https://github.com/utsab345/stepfork/tree/main/examples/langgraph_agent)
  — the same workflow with two tools, a behavioral diff, and integrity
  verification between the two runs.
- [Exporting pytest tests](../pytest-export.md) — trajectory expectations,
  replay modes, and what the generated test asserts.
- [Trace format](../trace-format.md) — the event and manifest contract this
  bundle follows.
