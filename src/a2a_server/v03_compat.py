"""Narrow A2A v0.3 compatibility fixes required by the application contract."""

from typing import Any

from a2a.compat.v0_3 import types as types_v03
from a2a.compat.v0_3.jsonrpc_adapter import JSONRPC03Adapter
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.types import TaskNotFoundError
from starlette.responses import JSONResponse
from starlette.routing import Route


class AdCPJSONRPC03Adapter(JSONRPC03Adapter):
    """Preserve task-not-found semantics that the SDK v0.3 adapter flattens."""

    async def _process_non_streaming_request(
        self,
        request_id: str | int | None,
        request_obj: Any,
        context: ServerCallContext,
    ) -> JSONResponse:
        try:
            return await super()._process_non_streaming_request(request_id, request_obj, context)
        except TaskNotFoundError as error:
            error_response = types_v03.JSONRPCErrorResponse(
                id=request_id,
                error=types_v03.TaskNotFoundError(message=str(error), data=error.data),
            )
            response: types_v03.GetTaskResponse | types_v03.CancelTaskResponse
            if request_obj.method == "tasks/get":
                response = types_v03.GetTaskResponse(root=error_response)
            elif request_obj.method == "tasks/cancel":
                response = types_v03.CancelTaskResponse(root=error_response)
            else:
                raise
            return JSONResponse(content=response.model_dump(mode="json", by_alias=True, exclude_none=True))


class AdCPJsonRpcDispatcher(JsonRpcDispatcher):
    """SDK dispatcher with the application-owned v0.3 task-error adapter."""

    def __init__(
        self,
        request_handler: RequestHandler,
        context_builder: ServerCallContextBuilder | None = None,
        enable_v0_3_compat: bool = False,
    ) -> None:
        super().__init__(
            request_handler=request_handler,
            context_builder=context_builder,
            enable_v0_3_compat=enable_v0_3_compat,
        )
        if enable_v0_3_compat:
            self._v03_adapter = AdCPJSONRPC03Adapter(
                http_handler=request_handler,
                context_builder=self._context_builder,
            )


def create_compatible_jsonrpc_routes(
    request_handler: RequestHandler,
    rpc_url: str,
    context_builder: ServerCallContextBuilder | None = None,
    enable_v0_3_compat: bool = False,
) -> list[Route]:
    """Create A2A JSON-RPC routes with application-owned v0.3 compatibility."""
    dispatcher = AdCPJsonRpcDispatcher(
        request_handler=request_handler,
        context_builder=context_builder,
        enable_v0_3_compat=enable_v0_3_compat,
    )
    return [Route(path=rpc_url, endpoint=dispatcher.handle_requests, methods=["POST"])]
