"""Framework-agnostic error type. Services raise ApiError; main.py turns it into a JSON response."""


class ApiError(Exception):
    def __init__(self, status_code: int, detail, code: str | None = None):
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.status_code = status_code
        self.detail = detail
        self.code = code

    def to_body(self) -> dict:
        body = {"detail": self.detail}
        if self.code:
            body["code"] = self.code
        return body


def not_found(detail: str, code: str = "not_found") -> ApiError:
    return ApiError(404, detail, code)


def unprocessable(detail: str, code: str = "invalid_request") -> ApiError:
    return ApiError(422, detail, code)
