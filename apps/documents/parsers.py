"""Bounded multipart parser for document uploads.

Wraps the request stream so the 10MB upload cap is enforced *as the
body streams in*, before Django/DRF buffer it to memory or temp files.
Content-Length headers can lie (or be absent under chunked encoding),
so the check must be on the actual bytes received.
"""

from rest_framework.exceptions import APIException
from rest_framework.parsers import MultiPartParser

from .services import MAX_UPLOAD_BYTES

# Cap includes multipart framing overhead (boundaries, headers) so a
# legitimate file at the size limit still parses.
_STREAM_CAP_BYTES = MAX_UPLOAD_BYTES + 1024


class UploadTooLarge(APIException):
    status_code = 413
    default_detail = (
        "Document exceeds the 10MB limit. Please upload a smaller photo."
    )
    default_code = "upload_too_large"


class _BoundedStream:
    """Read-through stream that aborts once more than `cap` bytes flow."""

    def __init__(self, stream, cap: int):
        self._stream = stream
        self._cap = cap
        self._total = 0

    def read(self, n=-1):
        data = self._stream.read(n)
        self._total += len(data)
        if self._total > self._cap:
            raise UploadTooLarge()
        return data

    def __getattr__(self, name):
        return getattr(self._stream, name)


class BoundedMultiPartParser(MultiPartParser):
    def parse(self, stream, media_type, parser_context):
        return super().parse(
            _BoundedStream(stream, _STREAM_CAP_BYTES),
            media_type=media_type,
            parser_context=parser_context,
        )
