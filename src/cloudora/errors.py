"""The one error type Cloudora raises for refusals. ``message`` is shown to the visitor, so it never holds secrets."""

from __future__ import annotations


class Codes:
    BAD_REQUEST = "bad_request"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"


class CloudoraError(Exception):
    def __init__(self, code: str, message: str = "", http_status: int = 400):
        self.code = code
        self.message = message or code.replace("_", " ")
        self.http_status = http_status
        super().__init__(f"{code}: {self.message}")
