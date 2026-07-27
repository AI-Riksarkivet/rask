from pydantic import BaseModel


class Health(BaseModel):
    status: str
    input: str
    output: str
