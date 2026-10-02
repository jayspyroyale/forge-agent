"""The agent loop.

    task -> model -> tool calls? --no--> final answer
                        |
                       yes -> ToolExecutor -> ToolResult -> back into the conversation -> model again

The agent only speaks Forge's normalized types: it does not know which model
provider it is talking to, and it never runs a tool itself. Every tool call
goes through the `ToolExecutor`.
"""

from forge.agent.events import (
    AgentEvent,
    AgentFinished,
    EventHandler,
    ModelRequested,
    ModelResponded,
    TaskStarted,
    ToolFinished,
    ToolStarted,
)
from forge.agent.state import AgentState, AgentStatus, ToolExecution
from forge.models.base import ModelProvider
from forge.models.errors import ModelError
from forge.models.types import Message, ModelResponse, ToolCall
from forge.tools.executor import ToolExecutor

DEFAULT_MAX_STEPS = 20


class Agent:
    def __init__(
        self,
        provider: ModelProvider,
        executor: ToolExecutor,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        system_prompt: str | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.provider = provider
        self.executor = executor
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.on_event = on_event

    async def run(self, task: str) -> AgentState:
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
                break
            await self._step(state)

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
        response = await self._ask_model(state)
        if response is None:
            return

        state.messages.append(
            Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        )
        if not response.tool_calls:
            state.status = AgentStatus.COMPLETED
            state.final_answer = response.content
            return

        for call in response.tool_calls:
            await self._run_tool(state, call)

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
        state.messages.append(
            Message(role="tool", content=result.to_model_content(), tool_call_id=call.id)
        )
        self._emit(ToolFinished(step=state.step, call=call, result=result))

    def _emit(self, event: AgentEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)
