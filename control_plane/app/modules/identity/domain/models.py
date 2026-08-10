from control_plane.app.shared.api.camel import CamelModel


class Principal(CamelModel):
    employee_id: str
    name: str


class NavigationItem(CamelModel):
    route_key: str
    name: str
    order: int
