"""Distinguishable error types for the multipart parser.

Every error carries a stable ``code`` string (useful for mapping to HTTP
status codes) plus the offending ``limit`` / observed ``got`` value when
applicable.
"""


class MultipartError(Exception):
    code = "multipart_error"

    def __init__(self, message="", *, limit=None, got=None):
        super().__init__(message)
        self.limit = limit
        self.got = got


class InvalidBoundary(MultipartError):
    """The boundary configured by the caller is not RFC-compliant."""

    code = "invalid_boundary"


class MalformedForm(MultipartError):
    """The body does not follow the multipart/form-data syntax."""

    code = "malformed_form"


class UnexpectedEOF(MultipartError):
    """Input ended before the closing boundary delimiter was seen."""

    code = "unexpected_eof"


class TooManyParts(MultipartError):
    code = "too_many_parts"


class HeadersTooLarge(MultipartError):
    """A part's header block exceeds max_header_size."""

    code = "headers_too_large"


class FieldTooLarge(MultipartError):
    """A non-file field value exceeds max_field_size."""

    code = "field_too_large"


class FileTooLarge(MultipartError):
    """A single uploaded file exceeds max_file_size."""

    code = "file_too_large"


class TotalSizeExceeded(MultipartError):
    """Cumulative body size exceeds max_total_size."""

    code = "total_size_exceeded"
