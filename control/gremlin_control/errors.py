from fastapi import HTTPException


def api_error(status_code: int, code: str, message: str, *, reason=None, headers=None):
    """Every Gremlin error is `{"detail": {"code", "message"}}`, plus `reason` when typed."""
    detail = {"code": code, "message": message}
    if reason is not None:
        detail["reason"] = reason
    return HTTPException(status_code=status_code, detail=detail, headers=headers)
