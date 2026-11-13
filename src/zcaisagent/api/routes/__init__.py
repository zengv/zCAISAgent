from zcaisagent.api.routes.admin import router as admin_router
from zcaisagent.api.routes.chat import router as chat_router
from zcaisagent.api.routes.n2sql import router as n2sql_router
from zcaisagent.api.routes.runs import router as runs_router

__all__ = ["admin_router", "chat_router", "n2sql_router", "runs_router"]
