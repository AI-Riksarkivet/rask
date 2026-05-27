from pydantic import BaseModel


class PageEntry(BaseModel):
    key: str
    hasAlto: bool
