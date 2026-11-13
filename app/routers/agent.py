"""Agent wrapper: POST /api/v1/agent/instruct.

A plain-English instruction is parsed (rule-based) into an intent and parameters.
The agent then calls the matching endpoint of this same API in-process, and
returns the API's response together with a one-line summary.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from starlette.concurrency import run_in_threadpool

from app.agent.asgi_client import call_api
from app.agent.executor import plan_call, summarize
from app.agent.parser import EXAMPLES, Intent, InstructionError, parse
from app.errors import InstructionNotUnderstood, detail
from app.schemas import AgentApiCall, AgentApiResponse, AgentRequest, AgentResponse, ErrorResponse

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])
API_PREFIX = "/api/v1"


@router.post(
    "/instruct",
    response_model=AgentResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Run a plain-English instruction against the API",
)
async def instruct(body: AgentRequest, request: Request,
                   x_user: Annotated[str | None, Header(max_length=64)] = None) -> AgentResponse:
    """Returns 200 whenever the agent made an API call. The API's own outcome,
    success or error, is in `api_response`, and `summary` explains it.

    The agent fails on its own before calling the API in three cases. It returns
    422 `INSTRUCTION_NOT_UNDERSTOOD` when it can't parse the instruction (with
    examples) or a reference is ambiguous. It returns 404 when a vendor, PO or
    invoice it names doesn't exist. It returns 409 for "receive everything" when
    nothing is left.
    """
    actor = (x_user or "").strip() or "agent"
    try:
        instruction = parse(body.instruction)
    except InstructionError as e:
        raise InstructionNotUnderstood(
            e.message, [detail(e.hint, field="instruction")] if e.hint else [],
            data={"supported_intents": [i.value for i in Intent], "examples": EXAMPLES}) from e

    plan = await run_in_threadpool(plan_call, instruction, actor)
    result = await call_api(request.app, plan.method, API_PREFIX + plan.path, plan.body, {"X-User": actor})
    return AgentResponse(
        instruction=body.instruction,
        intent=instruction.intent,
        parameters=instruction.public_params(),
        api_call=AgentApiCall(method=plan.method, path=API_PREFIX + plan.path, body=plan.body),
        api_response=AgentApiResponse(status_code=result.status_code, body=result.body),
        summary=summarize(instruction.intent, plan, result),
    )
