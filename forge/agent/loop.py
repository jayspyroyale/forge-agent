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
"""

import asyncio
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
from forge.models.base import ModelProvider
from forge.models.errors import ModelError
from forge.models.types import Message, ModelResponse, ToolCall
from forge.tools.executor import ToolExecutor
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
        self._reset()

    def _reset(self) -> None:
        self._failures = FailureTracker()
        self._nudged_empty_reply = False
        self._changes_at_last_verification = 0

    async def run(self, task: str) -> AgentState:
        self._reset()
        state = AgentState(task=task)
        if self.system_prompt:
            state.messages.append(Message.system(self.system_prompt))
        state.messages.append(Message.user(task))
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
            await self._step(state)

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
            state.messages.append(Message.user(LAST_STEP_NOTE))
        response = await self._ask_model(state)
        if response is None:
            return

        state.messages.append(
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        )
        if response.tool_calls:
            for call in response.tool_calls:
                await self._run_tool(state, call)
            return

        if not response.content.strip() and not self._nudged_empty_reply:
            # An empty reply is usually a glitch, not a finished task: ask once more.
            self._nudged_empty_reply = True
            state.messages.append(Message.user(EMPTY_RESPONSE_NOTE))
            return
        await self._finish(state, response.content)

    async def _finish(self, state: AgentState, answer: str) -> None:
        """The model says it is done. Verify its changes before accepting that."""
        if self._needs_verification(state):
            round_ = await self._verify(state)
            if not round_.passed:
                retries_left = len(state.verification_rounds) < self.verification_attempts
                if retries_left and state.step < self.max_steps:
                    state.messages.append(Message.user(_failure_note(round_.results)))
                    return  # keep working
                state.status = AgentStatus.VERIFICATION_FAILED
                state.final_answer = answer
                state.error = "Verification still fails after the agent finished."
                return
        state.status = AgentStatus.COMPLETED
        state.final_answer = answer

    def _needs_verification(self, state: AgentState) -> bool:
        if self.verifier is None or not self.verifier.checks:
            return False
        return state.change_count() > self._changes_at_last_verification

    async def _verify(self, state: AgentState) -> VerificationRound:
        assert self.verifier is not None
        self._changes_at_last_verification = state.change_count()
        results = []
        for check in self.verifier.checks:
            self._emit(VerificationStarted(step=state.step, name=check.name, command=check.command))
            result = await asyncio.to_thread(self.verifier.run_check, check)
            results.append(result)
            self._emit(VerificationFinished(step=state.step, result=result))
        round_ = VerificationRound(step=state.step, results=results)
        state.verification_rounds.append(round_)
        return round_

    async def _ask_model(self, state: AgentState) -> ModelResponse | None:
        self._emit(ModelRequested(step=state.step, message_count=len(state.messages)))
        try:
            response = await self.provider.generate(
                state.messages, tools=self.executor.registry.definitions()
            )
        except ModelError as error:
            state.status = AgentStatus.FAILED
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
        state.messages.append(Message(role="tool", content=content, tool_call_id=call.id))
        self._emit(ToolFinished(step=state.step, call=call, result=result))

    def _emit(self, event: AgentEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)


def _failure_note(results: list[VerificationResult]) -> str:
    blocks = []
    for result in results:
        if not result.failing:
            continue
        exit_info = f", exit code {result.exit_code}" if result.exit_code is not None else ""
        summary = result.summary[-MAX_FAILURE_SUMMARY_CHARS:]
        blocks.append(f"- {result.name} (`{result.command}`): {result.status}{exit_info}\n{summary}")
    return VERIFICATION_FAILED_NOTE.format(failures="\n\n".join(blocks))
