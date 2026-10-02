"""The agent loop.

    task -> model -> tool calls? --no--> final answer -> (files changed? Forge verifies)
                        |                                     |
                       yes -> ToolExecutor -> ToolResult       +-- checks fail and retries left:
                        |                                     |     failure summary goes back to the model
                        +---- back into the conversation <----+

The agent only speaks Forge's normalized types: it does not know which model
provider it is talking to, and it never runs a tool itself. Every tool call
goes through the `ToolExecutor`, and verification is run by Forge's
`Verifier`, never by trusting what the model says.

Every message is recorded twice: in full in `state.messages` (the record),
and as a `ContextItem` with provenance in the context engine, which decides
what is actually sent to the model under the context budget.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from forge.agent.events import (
    AgentEvent,
    AgentFinished,
    EventHandler,
    ModelRequested,
    ModelResponded,
    TaskStarted,
    ToolFinished,
    ToolStarted,
    VerificationFinished,
    VerificationStarted,
)
from forge.agent.guidance import FailureTracker
from forge.agent.prompts import EMPTY_RESPONSE_NOTE, LAST_STEP_NOTE, VERIFICATION_FAILED_NOTE
from forge.agent.state import AgentState, AgentStatus, ToolExecution, VerificationRound
from forge.context.compress import summarize_output
from forge.context.engine import ContextBudget, ContextEngine
from forge.context.items import BackgroundItem, Importance, Provenance, SourceType
from forge.context.tokens import estimate_tokens
from forge.models.base import ModelProvider
from forge.models.budget import BudgetExhausted
from forge.models.errors import ModelError
from forge.models.types import Message, ModelResponse, ToolCall
from forge.tools.base import ToolResult
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolNotFoundError
from forge.verification.checks import VerificationResult
from forge.verification.runner import Verifier

DEFAULT_MAX_STEPS = 20
DEFAULT_VERIFICATION_ATTEMPTS = 3
MAX_FAILURE_SUMMARY_CHARS = 1500


class Agent:
    def __init__(
        self,
        provider: ModelProvider,
        executor: ToolExecutor,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        system_prompt: str | None = None,
        verifier: Verifier | None = None,
        verification_attempts: int = DEFAULT_VERIFICATION_ATTEMPTS,
        on_event: EventHandler | None = None,
        context_budget: ContextBudget | None = None,
        operation_guard: Callable[[], None] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.provider = provider
        self.executor = executor
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.verifier = verifier
        self.verification_attempts = verification_attempts
        self.on_event = on_event
        self.context_budget = context_budget or ContextBudget()
        self.operation_guard = operation_guard
        self._reset()

    def _reset(self) -> None:
        self._failures = FailureTracker()
        self._nudged_empty_reply = False
        self._changes_at_last_verification = 0
        self.context = ContextEngine(self.context_budget)

    async def run(
        self, task: str, task_id: str | None = None, background: Sequence[BackgroundItem] = ()
    ) -> AgentState:
        """Run `task`. `background` is context Forge gathered beforehand (relevant files, memory)."""
        self._reset()
        state = AgentState(task=task) if task_id is None else AgentState(task=task, task_id=task_id)
        if self.system_prompt:
            self._add(
                state,
                Message.system(self.system_prompt),
                Provenance(source=SourceType.SYSTEM, source_id="system_prompt"),
                importance=Importance.CRITICAL,
            )
        # Background goes before the task, so the task is the last thing the model reads.
        for item in background:
            self._add(state, Message.user(item.content), item.provenance, importance=item.importance, key=item.key)
        self._add(state, Message.user(task), Provenance(source=SourceType.USER, source_id="task"), importance=Importance.CRITICAL)
        self._emit(TaskStarted(task=task))

        while state.status == AgentStatus.RUNNING:
            if state.step >= self.max_steps:
                state.status = AgentStatus.MAX_STEPS
                state.error = (
                    f"Stopped after {self.max_steps} steps without a final answer. "
                    "Increase max_steps or break the task into smaller pieces."
                )
                if self._needs_verification(state):
                    # No retries left, but record what state the code was left in.
                    await self._verify(state)
                break
            try:
                self._guard()
                await self._step(state)
            except BudgetExhausted as error:
                state.status = AgentStatus.BUDGET_EXHAUSTED
                state.error = str(error)

        state.finished_at = datetime.now(UTC)
        self._emit(
            AgentFinished(
                status=state.status,
                steps=state.step,
                final_answer=state.final_answer,
                error=state.error,
            )
        )
        return state

    async def _step(self, state: AgentState) -> None:
        """One model call, plus any tool calls it requested."""
        state.step += 1
        if state.step == self.max_steps and self.max_steps > 1:
            self._add_note(state, LAST_STEP_NOTE, "note:last_step")
        response = await self._ask_model(state)
        if response is None:
            return

        self._add(
            state,
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls),
            Provenance(source=SourceType.MODEL, source_id=self.provider.name, step=state.step),
        )
        if response.tool_calls:
            for call in response.tool_calls:
                self._guard()
                await self._run_tool(state, call)
            return

        if not response.content.strip() and not self._nudged_empty_reply:
            # An empty reply is usually a glitch, not a finished task: ask once more.
            self._nudged_empty_reply = True
            self._add_note(state, EMPTY_RESPONSE_NOTE, "note:empty_reply")
            return
        await self._finish(state, response.content)

    async def _finish(self, state: AgentState, answer: str) -> None:
        """The model says it is done. Verify its changes before accepting that."""
        if self._needs_verification(state):
            round_ = await self._verify(state)
            if not round_.passed:
                retries_left = len(state.verification_rounds) < self.verification_attempts
                if retries_left and state.step < self.max_steps:
                    self._add(
                        state,
                        Message.user(_failure_note(round_.results)),
                        Provenance(source=SourceType.VERIFICATION, source_id="checks", step=state.step),
                        importance=Importance.HIGH,
                        key="note:verification",
                    )
                    return  # keep working
                state.status = AgentStatus.VERIFICATION_FAILED
                state.final_answer = answer
                state.error = "Verification still fails after the agent finished."
                return
        state.status = AgentStatus.COMPLETED
        state.final_answer = answer

    async def verify_final(self, state: AgentState) -> VerificationRound | None:
        """Verify once more after the loop, if the workspace may have changed since the last check.

        The loop only notices changes made by file-editing tools. A command
        (a formatter, a code generator, `sed -i`, ...) can change files too;
        the runtime calls this when its workspace comparison shows changes
        that no verification round has covered yet. A failure here cannot be
        sent back to the model anymore, so a completed task becomes
        `verification_failed`.
        """
        if self.verifier is None or not self.verifier.checks:
            return None
        if state.verification_rounds and state.verification_rounds[-1].step >= state.last_mutation_step():
            return None
        round_ = await self._verify(state)
        if not round_.passed and state.status == AgentStatus.COMPLETED:
            state.status = AgentStatus.VERIFICATION_FAILED
            state.error = "Forge's final verification failed after the agent finished."
        return round_

    def _needs_verification(self, state: AgentState) -> bool:
        if self.verifier is None or not self.verifier.checks:
            return False
        return state.change_count() > self._changes_at_last_verification

    async def _verify(self, state: AgentState) -> VerificationRound:
        assert self.verifier is not None
        self._changes_at_last_verification = state.change_count()
        results = []
        for check in self.verifier.checks:
            self._guard()
            self._emit(VerificationStarted(step=state.step, name=check.name, command=check.command))
            result = await asyncio.to_thread(self.verifier.run_check, check)
            results.append(result)
            self._emit(VerificationFinished(step=state.step, result=result))
        round_ = VerificationRound(step=state.step, results=results)
        state.verification_rounds.append(round_)
        return round_

    async def _ask_model(self, state: AgentState) -> ModelResponse | None:
        view = self.context.render()
        self._emit(ModelRequested(step=state.step, message_count=len(view.messages), context_tokens=view.tokens))
        try:
            response = await self.provider.generate(view.messages, tools=self.executor.registry.definitions())
        except ModelError as error:
            state.status = AgentStatus.BUDGET_EXHAUSTED if isinstance(error, BudgetExhausted) else AgentStatus.FAILED
            state.error = str(error)
            return None
        if response.usage is not None:
            state.usage = response.usage if state.usage is None else state.usage + response.usage
        self._emit(ModelResponded(step=state.step, response=response))
        return response

    async def _run_tool(self, state: AgentState, call: ToolCall) -> None:
        self._emit(ToolStarted(step=state.step, call=call))
        result = await self.executor.execute(call)
        state.tool_history.append(ToolExecution(step=state.step, call=call, result=result))
        content = result.to_model_content()
        note = self._failures.note_for(call, result)
        if note:
            content += "\n\n" + note
        self._add_tool_result(state, call, result, content)
        self._emit(ToolFinished(step=state.step, call=call, result=result))

    def _add(self, state: AgentState, message: Message, provenance: Provenance, **item) -> None:
        state.messages.append(message)
        self.context.add(message, provenance, **item)

    def _add_note(self, state: AgentState, text: str, key: str) -> None:
        provenance = Provenance(source=SourceType.SYSTEM, source_id="forge", step=state.step)
        self._add(state, Message.user(text), provenance, importance=Importance.LOW, key=key)

    def _add_tool_result(self, state: AgentState, call: ToolCall, result: ToolResult, content: str) -> None:
        try:
            tool = self.executor.registry.get(call.name)
        except ToolNotFoundError:
            tool = None
        source = SourceType(tool.context_source) if tool is not None else SourceType.TOOL_RESULT
        reference = tool.context_reference(call.arguments) if tool is not None else None
        provenance = Provenance(
            source=source,
            source_id=reference,
            step=state.step,
            tool_name=call.name,
            tool_call_id=call.id,
        )
        summary = None
        if estimate_tokens(content) > self.context_budget.compress_above_tokens:
            summary = summarize_output(call.name, call.arguments, content)
        self._add(
            state,
            Message(role="tool", content=content, tool_call_id=call.id),
            provenance,
            # Errors are what the model most needs to see again; keep them longer.
            importance=Importance.NORMAL if result.success else Importance.HIGH,
            key=_context_key(tool, call, reference),
            changes=result.metadata.get("changed_paths", []) if result.success else [],
            summary=summary,
        )

    def _emit(self, event: AgentEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def _guard(self) -> None:
        if self.operation_guard is not None:
            self.operation_guard()


def _context_key(tool, call: ToolCall, reference: str | None) -> str | None:
    """What a tool result is about, so a later result about the same thing can supersede it.

    Only repeatable observations get a key (reads, searches, commands); actions such as edits don't.
    """
    if tool is None:
        return None
    arguments = call.arguments
    if call.name == "read_file" and reference and not (arguments.get("start_line") or arguments.get("max_lines")):
        return "file:" + normalize_path(reference)
    if call.name == "run_command" and reference:
        return f"run_command:{reference}:{arguments.get('cwd', '.')}"
    if tool.risk == "read":
        return f"{call.name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
    return None


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _failure_note(results: list[VerificationResult]) -> str:
    blocks = []
    for result in results:
        if not result.failing:
            continue
        exit_info = f", exit code {result.exit_code}" if result.exit_code is not None else ""
        summary = result.summary[-MAX_FAILURE_SUMMARY_CHARS:]
        blocks.append(f"- {result.name} (`{result.command}`): {result.status}{exit_info}\n{summary}")
    return VERIFICATION_FAILED_NOTE.format(failures="\n\n".join(blocks))
