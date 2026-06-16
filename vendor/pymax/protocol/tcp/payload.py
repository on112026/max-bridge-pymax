from enum import Enum
from typing import Any

import msgpack

from pymax.logging import get_logger

from .compression import Lz4BlockCompression

logger = get_logger(__name__)


class MsgpackPayloadCodec:
    def _to_msgpack_value(self, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {self._to_msgpack_value(k): self._to_msgpack_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._to_msgpack_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._to_msgpack_value(item) for item in value)
        return value

    def encode(self, payload: object) -> bytes:
        if payload is None:
            return b""
        return msgpack.packb(self._to_msgpack_value(payload), use_bin_type=True) or b""

    def _unpack_stream(
        self, payload_bytes: bytes, *, raw: bool
    ) -> list[Any]:  # TODO: deprecate? idk
        unpacker = msgpack.Unpacker(raw=raw, strict_map_key=False)
        unpacker.feed(payload_bytes)
        return list(unpacker)

    def decode(self, payload_bytes: bytes) -> Any:
        if not payload_bytes:
            return {}

        try:
            return msgpack.unpackb(
                payload_bytes,
                raw=False,
                strict_map_key=False,
            )

        except msgpack.exceptions.ExtraData as e:
            logger.debug(
                "msgpack extra data: unpacked_type=%s extra_len=%s extra_head=%s payload_head=%s",
                type(e.unpacked).__name__,
                len(e.extra),
                e.extra[:64].hex(),
                payload_bytes[:128].hex(),
            )
            return e.unpacked

        except Exception:
            logger.exception(
                "msgpack decode failed: payload_len=%s payload_head=%s",
                len(payload_bytes),
                payload_bytes[:128].hex(),
            )
            raise


class TcpPayloadDecoder:
    def __init__(
        self,
        *,
        serializer: MsgpackPayloadCodec,
        compression: Lz4BlockCompression | None = None,
    ) -> None:
        self.serializer = serializer
        self.compression = compression

    def _normalize_keys(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {self._normalize_key(k): self._normalize_keys(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._normalize_keys(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self._normalize_keys(item) for item in obj)
        return obj

    def _normalize_key(self, key: Any) -> Any:
        if isinstance(key, int):
            return str(key)
        if isinstance(key, bytes):
            try:
                return key.decode("utf-8")
            except UnicodeDecodeError:
                return key.hex()
        return key

    def decode(self, payload_bytes: bytes, flags: int = 0) -> dict[str, Any]:
        if not payload_bytes:
            return {}

        if flags & 0x03 and self.compression:
            try:
                payload_bytes = self.compression.decompress(payload_bytes)
                logger.debug("tcp payload decompressed flags=%s", flags)
            except ValueError:
                logger.debug("tcp payload decompress skipped flags=%s", flags)

        result = self.serializer.decode(payload_bytes)
        return self._normalize_keys(result)
