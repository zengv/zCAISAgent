from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Response
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import N2SQLExportRequest, N2SQLRequest, N2SQLResponse

router = APIRouter()

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_EXPORT_ROWS = 5000

_ERROR_STATUS = {
    "N2SQL_FORBIDDEN": 403,
    "N2SQL_INVALID_ARGS": 400,
    "N2SQL_INVALID_SQL": 400,
    "N2SQL_LLM_FAILED": 502,
    "N2SQL_DB_FAILED": 502,
}


def _build_workbook(request: N2SQLExportRequest) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "查询结果"
    sheet.append(request.columns)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in request.rows:
        values = [row.get(column) for column in request.columns]
        sheet.append(["" if value is None else value for value in values])
        # openpyxl 会把以 = 开头的字符串识别为公式；导出数据一律按文本落盘。
        for cell, value in zip(sheet[sheet.max_row], values, strict=True):
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                cell.data_type = "s"

    for index, column in enumerate(request.columns, start=1):
        longest = max(
            [len(str(column))]
            + [len(str(row.get(column, ""))) for row in request.rows[:200]]
        )
        sheet.column_dimensions[get_column_letter(index)].width = min(
            max(longest + 4, 10), 42
        )
    sheet.freeze_panes = "A2"

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@router.post("/n2sql", response_model=N2SQLResponse)
async def ask_n2sql(
    request: N2SQLRequest,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> N2SQLResponse:
    container = get_container()
    auth = await container.identity.authenticate(
        x_api_key,
        {"tenant_id": request.tenant_id or "", "user_id": request.user_id},
    )
    service = container.n2sql
    if service is None:
        raise HTTPException(status_code=503, detail="智能问数未启用或数据库未配置")

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

    result = await service.ask(question, auth)
    if not result.ok:
        status_code = _ERROR_STATUS.get(result.error_code, 400)
        raise HTTPException(
            status_code=status_code,
            detail=result.error_message or result.error_code,
        )
    return N2SQLResponse(
        question=question,
        answer=result.answer,
        sql=result.sql,
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        scope=result.scope,
        latency_ms=result.latency_ms,
    )


@router.post("/n2sql/export")
async def export_n2sql(
    request: N2SQLExportRequest,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> Response:
    container = get_container()
    await container.identity.authenticate(
        x_api_key,
        {"tenant_id": request.tenant_id or "", "user_id": request.user_id},
    )
    if not request.columns:
        raise HTTPException(status_code=400, detail="columns must not be empty")
    if not request.rows:
        raise HTTPException(status_code=400, detail="rows must not be empty")
    if len(request.rows) > _MAX_EXPORT_ROWS:
        raise HTTPException(status_code=400, detail="too many rows to export")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"问数结果_{stamp}.xlsx"
    return Response(
        content=_build_workbook(request),
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": (
                f'attachment; filename="n2sql_{stamp}.xlsx"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
