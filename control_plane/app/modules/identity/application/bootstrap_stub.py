from control_plane.app.modules.identity.domain.models import NavigationItem, Principal


def get_me() -> Principal:
    return Principal(employee_id="00000000", name="V0.1 Stub")


def get_navigation() -> list[NavigationItem]:
    return [
        NavigationItem(route_key="home", name="首页", order=1),
        NavigationItem(route_key="admin", name="管理后台", order=2),
    ]
