"""Error de aplicación con código estable para la interfaz."""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, code: str, status: int, message: str, detail: str = ""):
        super().__init__(message)
        self.code, self.status, self.message, self.detail = code, status, message, detail
