from pydantic import BaseModel


class ProjectDTO(BaseModel):
    slug: str
    name: str
    team: str
    workload: str
    phase: str
    namespace: str
    url: str
    created_at: str


class ProjectsResponse(BaseModel):
    projects: list[ProjectDTO]
