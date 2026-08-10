from typing import Self

from control_plane.app.modules.identity.domain.models import NavigationItem, Principal
from control_plane.app.shared.api.camel import CamelModel


class PrincipalDto(CamelModel):
    employee_id: str
    name: str

    @classmethod
    def from_domain(cls, principal: Principal) -> Self:
        return cls(employee_id=principal.employee_id, name=principal.name)


class NavigationItemDto(CamelModel):
    route_key: str
    name: str
    order: int

    @classmethod
    def from_domain(cls, item: NavigationItem) -> Self:
        return cls(route_key=item.route_key, name=item.name, order=item.order)
