"""runtime info 只读端点（V15-002）。

直接返回 application.state.runtime.info（装配时生成的安全七键 dict）；
本层不 inspect sys.platform、不组装路径、不接触凭据或 env 值。
"""

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])


@router.get("/info")
async def runtime_info(request: Request) -> dict[str, object]:
    return request.app.state.runtime.info
