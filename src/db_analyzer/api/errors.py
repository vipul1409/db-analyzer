"""The API's one error shape, and how domain exceptions map onto it."""

from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from db_analyzer.core.model import (
    ConnectionRefused,
    DsnEnvMissing,
    GateRejected,
    QueryRejected,
    UnknownCollections,
)
from db_analyzer.runs import OptionsNotAccepted, UnknownAnalyzer
from db_analyzer.service import RunsNotComparable, TurnActive


class ErrorBody(BaseModel):
    """Every error: a stable machine `code`, a `message` to show, and `details` when there is
    more to say."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = ErrorBody(code=code, message=message, details=details)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorBody} for status in (401, 404, 409, 422, 502, 503)
}

_HTTP_CODES = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}


def install(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    for exc in _DOMAIN:
        app.add_exception_handler(exc, _domain_error)
    app.add_exception_handler(Exception, _unexpected_error)


def to_api_error(e: Exception) -> ApiError:
    """The API error for a domain exception (see `_DOMAIN` for which ones are mapped)."""
    match e:
        case KeyError():
            return ApiError(404, "not_found", str(e.args[0]) if e.args else "not found")
        case UnknownAnalyzer():
            return ApiError(422, "unknown_analyzer", str(e), {"analyzer": e.name})
        case OptionsNotAccepted():
            details: dict[str, Any] = {"options": e.options, "analyzer": e.analyzer}
            return ApiError(422, "options_not_accepted", str(e), details)
        case UnknownCollections():
            return ApiError(422, "unknown_collections", str(e), {"collections": e.names})
        case RunsNotComparable():
            return ApiError(422, "runs_not_comparable", str(e))
        case TurnActive():
            return ApiError(409, "turn_active", str(e))
        case DsnEnvMissing():
            return ApiError(422, "dsn_env_missing", str(e), {"dsn_env": e.env})
        case ConnectionRefused():
            return ApiError(422, "connection_refused", str(e))
        case GateRejected():
            details = {"reason": e.reason, "metric": e.metric, "value": e.value, "limit": e.limit}
            return ApiError(422, "query_rejected", e.reason, details)
        case QueryRejected():
            return ApiError(422, "query_rejected", e.reason, {"reason": e.reason})
        case psycopg.OperationalError():
            return ApiError(502, "database_unreachable", str(e).strip())
        case psycopg.Error():
            return ApiError(502, "database_error", str(e).strip())
    raise TypeError(f"no API error for {type(e).__name__}")


_DOMAIN: tuple[type[Exception], ...] = (
    KeyError,
    UnknownAnalyzer,
    OptionsNotAccepted,
    UnknownCollections,
    RunsNotComparable,
    TurnActive,
    ConnectionRefused,
    QueryRejected,
    psycopg.Error,
)


def _response(e: ApiError) -> JSONResponse:
    return JSONResponse(e.body.model_dump(), status_code=e.status)


async def _api_error(request: Request, e: Exception) -> JSONResponse:
    assert isinstance(e, ApiError)
    return _response(e)


async def _domain_error(request: Request, e: Exception) -> JSONResponse:
    return _response(to_api_error(e))


async def _unexpected_error(request: Request, e: Exception) -> JSONResponse:
    """A bug, in the same shape as any other error; the traceback goes to the server log."""
    return _response(ApiError(500, "internal_error", f"internal error: {type(e).__name__}"))


async def _http_error(request: Request, e: Exception) -> JSONResponse:
    assert isinstance(e, HTTPException)
    code = _HTTP_CODES.get(e.status_code, f"http_{e.status_code}")
    error = ApiError(e.status_code, code, str(e.detail))
    response = _response(error)
    response.headers.update(e.headers or {})
    return response


async def _validation_error(request: Request, e: Exception) -> JSONResponse:
    assert isinstance(e, RequestValidationError)
    errors = jsonable_encoder(
        [{k: err[k] for k in ("loc", "msg", "type") if k in err} for err in e.errors()]
    )
    return _response(ApiError(422, "invalid_request", "invalid request", {"errors": errors}))
