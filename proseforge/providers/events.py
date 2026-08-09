from enum import StrEnum


class GenerationEventType(StrEnum):
    RESPONSE_STARTED = "response.started"
    CONTENT_DELTA = "content.delta"
    REASONING_SUMMARY_DELTA = "reasoning.summary.delta"
    TOOL_CALL_STARTED = "tool_call.started"
    TOOL_CALL_ARGUMENTS_DELTA = "tool_call.arguments.delta"
    TOOL_CALL_COMPLETED = "tool_call.completed"
    USAGE_UPDATED = "usage.updated"
    RESPONSE_COMPLETED = "response.completed"
    RESPONSE_FAILED = "response.failed"
