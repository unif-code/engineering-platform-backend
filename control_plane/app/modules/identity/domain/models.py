from pydantic import BaseModel


class Principal(BaseModel):
    employee_id: str
    name: str


class NavigationItem(BaseModel):
    route_key: str
    name: str
    order: int
