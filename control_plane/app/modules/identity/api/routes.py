from fastapi import APIRouter

from control_plane.app.modules.identity.application.bootstrap_stub import get_me, get_navigation
from control_plane.app.modules.identity.domain.models import NavigationItem, Principal

router = APIRouter(prefix="/api/v1", tags=["identity"])

STUB_NOTE = "V0.1 技术性 stub：无认证语义，V0.2 起由真实 Session 与服务端授权判定原地替换。"


@router.get("/me", operation_id="identity_me", description=STUB_NOTE)
async def me() -> Principal:
    return get_me()


@router.get("/navigation", operation_id="identity_navigation", description=STUB_NOTE)
async def navigation() -> list[NavigationItem]:
    return get_navigation()
