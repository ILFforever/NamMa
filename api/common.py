"""Shared pieces of the web API contract (docs/api.md): envelope, errors, DB dependency."""
from collections.abc import Iterator

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from common import db

ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
               422: "bad_request", 429: "rate_limited", 503: "unavailable"}


class ApiError(Exception):
    """Raise for any contract error: ApiError(404, "not_found", "station 12 not found")."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def install_error_handlers(app: FastAPI) -> None:
    """Every /api error leaves as {"error": {"code", "message"}} (docs/api.md, Conventions)."""

    def body(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    @app.exception_handler(ApiError)
    async def _api(_req: Request, e: ApiError):
        return body(e.status, e.code, e.message)

    @app.exception_handler(HTTPException)
    async def _http(_req: Request, e: HTTPException):
        msg = e.detail if isinstance(e.detail, str) else str(e.detail)
        return body(e.status_code, ERROR_CODES.get(e.status_code, "error"), msg)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_req: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
        return body(400, "bad_request", f"{where}: {first.get('msg', 'invalid request')}".strip(": "))


def get_db() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: one connection per request, dict rows, Bangkok time so timestamps carry +07:00.
    Commits when the handler returns, rolls back if it raises."""
    with db.connect() as conn:
        conn.row_factory = dict_row
        conn.execute("SET TIME ZONE 'Asia/Bangkok'")
        yield conn


def ingested_at(conn: psycopg.Connection):
    """When ThaiWater was last ingested: the risk job runs right after every ingest."""
    row = conn.execute("SELECT max(computed_at) AS t FROM station_risk").fetchone()
    return row["t"] if isinstance(row, dict) else row[0]


def envelope(conn: psycopg.Connection, data) -> dict:
    """Success body: {"data": ..., "meta": {"ingested_at": ...}}."""
    return {"data": data, "meta": {"ingested_at": ingested_at(conn)}}
