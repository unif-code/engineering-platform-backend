from fastapi import APIRouter

from control_plane.app.modules.identity.api.dto import NavigationItemDto, PrincipalDto
from control_plane.app.modules.identity.application.bootstrap_stub import get_me, get_navigation

router = APIRouter(prefix="/api/v1", tags=["identity"])

STUB_NOTE = "V0.1 技术性 stub：无认证语义，V0.2 起由真实 Session 与服务端授权判定原地替换。"


@router.get("/me", operation_id="identity_me", description=STUB_NOTE)
async def me() -> PrincipalDto:
    return PrincipalDto.from_domain(get_me())


@router.get("/navigation", operation_id="identity_navigation", description=STUB_NOTE)
async def navigation() -> list[NavigationItemDto]:
    return [NavigationItemDto.from_domain(item) for item in get_navigation()]
