from fastapi import Header, HTTPException
from .config import settings


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.rag_api_key:
        raise HTTPException(status_code=401, detail="Invalid bearer token")