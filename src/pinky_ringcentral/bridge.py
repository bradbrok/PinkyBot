"""Private FastAPI bridge: credentials stay on the Mini, tools stay on the Pi."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pinky_ringcentral.auth import AgentIdentity, DerivedKeyAuthenticator
from pinky_ringcentral.client import RingCentralAPIError
from pinky_ringcentral.inbound import RingCentralInboundWorker
from pinky_ringcentral.service import (
    DEFAULT_RECIPIENT_TIMEZONE,
    SmsService,
    SmsServiceError,
)


class SendSmsRequest(BaseModel):
    to: str
    text: str
    template_id: str = ""
    recipient_timezone: str = DEFAULT_RECIPIENT_TIMEZONE


class DoNotTextAddRequest(BaseModel):
    phone_number: str
    reason: str = Field(default="manual", max_length=200)


class DoNotTextRemoveRequest(BaseModel):
    phone_number: str


def create_app(
    service: SmsService,
    authenticator: DerivedKeyAuthenticator,
    *,
    worker: RingCentralInboundWorker | None = None,
) -> FastAPI:
    """Build an injectable bridge app without reading process credentials."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        tasks: list[asyncio.Task[None]] = []
        if worker is not None:
            tasks = [
                asyncio.create_task(
                    worker.run_forever(),
                    name="ringcentral-inbound",
                ),
                asyncio.create_task(
                    worker.run_housekeeping_forever(),
                    name="ringcentral-housekeeping",
                ),
            ]
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await service.client.aclose()
            close_wake = getattr(service.wake_sink, "aclose", None)
            if close_wake is not None:
                await close_wake()
            service.store.close()

    app = FastAPI(
        title="Pinky RingCentral bridge",
        version="1.0",
        lifespan=lifespan,
    )

    async def identity(request: Request) -> AgentIdentity:
        body = await request.body()
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        verified = authenticator.authenticate(
            method=request.method,
            path=target,
            headers=request.headers,
            body=body,
        )
        if verified is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            try:
                consumed = service.store.consume_request_id(
                    verified.request_id,
                    agent_name=verified.name,
                    method=request.method.upper(),
                    target=target,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="authorization replay state unavailable",
                ) from exc
            if not consumed:
                raise HTTPException(status_code=401, detail="unauthorized")
        return verified

    @app.exception_handler(SmsServiceError)
    async def service_error_handler(
        _request: Request, exc: SmsServiceError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict())

    @app.exception_handler(RingCentralAPIError)
    async def ringcentral_error_handler(
        _request: Request, exc: RingCentralAPIError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": "ringcentral_api_error",
                "message": str(exc),
                "upstream_status": exc.status_code,
                "may_have_completed": exc.may_have_completed,
            },
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "auth_configured": authenticator.configured,
            "ringcentral_configured": service.client.configured,
            "inbound_mode": worker.mode if worker is not None else "disabled",
            "inbound_last_error": worker.last_error if worker is not None else "",
        }

    @app.post("/v1/sms/send")
    async def send_sms(
        body: SendSmsRequest,
        _identity: AgentIdentity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.send_sms(
            to=body.to,
            text=body.text,
            template_id=body.template_id,
            recipient_timezone=body.recipient_timezone,
        )

    @app.get("/v1/sms/history")
    async def get_sms_history(
        phone_number: str = Query(...),
        limit: int = Query(100),
        days: int = Query(30),
        _identity: AgentIdentity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.get_sms_history(
            phone_number=phone_number, limit=limit, days=days
        )

    @app.get("/v1/sms/{message_id}/status")
    async def get_sms_status(
        message_id: str,
        _identity: AgentIdentity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.get_sms_status(message_id=message_id)

    @app.post("/v1/do-not-text/add")
    async def add_do_not_text(
        body: DoNotTextAddRequest,
        caller: AgentIdentity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.add_do_not_text(
            phone_number=body.phone_number,
            reason=body.reason,
            source=f"manual:{caller.name}",
        )

    @app.post("/v1/do-not-text/remove")
    async def remove_do_not_text(
        body: DoNotTextRemoveRequest,
        _identity: AgentIdentity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.remove_do_not_text(
            phone_number=body.phone_number
        )

    @app.get("/v1/do-not-text")
    async def list_do_not_text(
        _identity: AgentIdentity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.list_do_not_text()

    return app
