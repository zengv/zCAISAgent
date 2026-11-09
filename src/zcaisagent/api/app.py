from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from zcaisagent.api.deps import get_container
from zcaisagent.api.routes import chat_router, runs_router
from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.ports.health import HealthStatus

_CHAT_PAGE = Path(__file__).parent / "static" / "chat.html"


def create_app() -> FastAPI:
    app = FastAPI(title="zCAISAgent", version="0.1.0")
    app.include_router(chat_router, prefix="/v1", tags=["chat"])
    app.include_router(runs_router, prefix="/v1", tags=["runs"])

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_CHAT_PAGE, media_type="text/html")

    @app.exception_handler(ZCAISAgentError)
    async def zcaisagent_error_handler(request: Request, exc: ZCAISAgentError):
        status_code = 401 if exc.code == "UNAUTHORIZED" else 400
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.get("/health")
    async def health() -> dict:
        container = get_container()
        entries = container.health.snapshot()
        healthy = all(entry.status == HealthStatus.OK for entry in entries)
        return {
            "status": "ok" if healthy else "degraded",
            "service": "zcaisagent",
            "entries": [entry.model_dump() for entry in entries],
        }

    return app


app = create_app()
