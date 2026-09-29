"""Read-only client for the Starlink dish's local gRPC API.

The dish serves ``SpaceX.API.Device.Device/Handle`` on 192.168.100.1:9200
with server reflection enabled, so we ask the dish itself for its message
definitions instead of shipping .proto files. Dishes across the fleet run
different hardware and firmware (and SpaceX renames and removes fields), so
this keeps working wherever reflection does, and callers treat every field
as optional.

Only read requests are ever sent. The same API can reboot or stow the dish
and change its config; nothing here builds those requests.

grpc is imported lazily: this runs inside the web container, which also
sends the heartbeat, and a missing or broken grpc install must degrade to
"Starlink unavailable" rather than stop that container starting.
"""

from __future__ import annotations

import threading
from typing import Optional

SERVICE = "SpaceX.API.Device.Device"
METHOD = f"/{SERVICE}/Handle"

# Requests we're willing to send. Anything else is refused before it reaches
# the dish, so a caller can't be tricked into sending a reboot.
READ_ONLY_REQUESTS = frozenset({
    "getStatus", "getHistory", "getLocation", "getDeviceInfo",
    "getDiagnostics",        # overage_rate_limited, disablement code, location enabled
    "getNetworkInterfaces",  # router WAN byte counters (data meter)
})


class StarlinkError(Exception):
    """A dish request failed. ``kind`` is one of:

    unreachable   — nothing answered at the address (no dish, wrong address)
    not_permitted — the dish refused (e.g. location access not enabled)
    unsupported   — this dish/firmware doesn't know the request or service
    unavailable   — grpc isn't installed in this image
    error         — anything else
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class StarlinkClient:
    def __init__(self, address: str = "192.168.100.1:9200", timeout: float = 5.0):
        self.address = address
        self.timeout = timeout
        self._channel = None
        self._request_cls = None
        self._call = None
        self.api_version: Optional[str] = None

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
        self._channel = None
        self._request_cls = None
        self._call = None

    def handle(self, request: dict) -> dict:
        """Send one read-only request, e.g. ``{"getStatus": {}}``; return the
        response as a dict with the same camelCase names grpcurl prints."""
        names = set(request)
        if not names or not names <= READ_ONLY_REQUESTS:
            raise ValueError(f"refusing non-read-only Starlink request: {sorted(names)}")

        try:
            import grpc
            from google.protobuf import json_format
        except ImportError as e:  # image built without grpc
            raise StarlinkError("unavailable", f"grpc not installed: {e}")

        try:
            self._ensure_ready(grpc)
            try:
                message = json_format.ParseDict(request, self._request_cls())
            except json_format.ParseError as e:
                raise StarlinkError("unsupported", f"dish doesn't know {sorted(names)}: {e}")
            response = self._call(message, timeout=self.timeout)
            result = json_format.MessageToDict(response)
            self.api_version = result.get("apiVersion", self.api_version)
            return result
        except StarlinkError:
            raise
        except grpc.RpcError as e:
            self._drop_channel_if_dead(e, grpc)
            raise _classify(e, grpc)
        except Exception as e:
            self.close()
            raise StarlinkError("error", str(e))

    # ── internals ───────────────────────────────────────────────────────────

    def _ensure_ready(self, grpc) -> None:
        if self._call is not None:
            return

        self._channel = grpc.insecure_channel(self.address)
        try:
            grpc.channel_ready_future(self._channel).result(timeout=self.timeout)
        except grpc.FutureTimeoutError:
            self.close()
            raise StarlinkError("unreachable", f"no answer from {self.address}")

        # The reflection client has no per-call deadline, so a dish that
        # accepts the connection and then goes quiet would hang this thread.
        # Closing the channel cancels the lookup; the timer does that for us.
        channel = self._channel
        watchdog = threading.Timer(self.timeout * 2, channel.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            request_cls, response_cls = _load_message_classes(channel)
        except KeyError:
            self.close()
            raise StarlinkError("unsupported", f"{self.address} doesn't serve {SERVICE}")
        except grpc.RpcError as e:
            self.close()
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise StarlinkError("unsupported", "dish doesn't support gRPC reflection")
            raise _classify(e, grpc)
        finally:
            watchdog.cancel()

        self._request_cls = request_cls
        self._call = channel.unary_unary(
            METHOD,
            request_serializer=request_cls.SerializeToString,
            response_deserializer=response_cls.FromString,
        )

    def _drop_channel_if_dead(self, e, grpc) -> None:
        # Rebuild after a transport failure so the next poll re-learns the
        # schema — the dish may have rebooted onto new firmware.
        if e.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
                        grpc.StatusCode.CANCELLED, grpc.StatusCode.UNKNOWN):
            self.close()


def _load_message_classes(channel):
    """Fetch Request/Response definitions from the dish over reflection."""
    from google.protobuf import message_factory
    from google.protobuf.descriptor_pool import DescriptorPool
    from grpc_reflection.v1alpha.proto_reflection_descriptor_database import (
        ProtoReflectionDescriptorDatabase,
    )

    pool = DescriptorPool(ProtoReflectionDescriptorDatabase(channel))
    service = pool.FindServiceByName(SERVICE)
    method = service.FindMethodByName("Handle")
    if method is None:
        raise KeyError("Handle")
    return (
        message_factory.GetMessageClass(method.input_type),
        message_factory.GetMessageClass(method.output_type),
    )


def _classify(e, grpc) -> StarlinkError:
    code = e.code()
    detail = e.details() or str(code)
    if code == grpc.StatusCode.PERMISSION_DENIED:
        return StarlinkError("not_permitted", detail)
    if code == grpc.StatusCode.UNIMPLEMENTED:
        return StarlinkError("unsupported", detail)
    if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.CANCELLED):
        return StarlinkError("unreachable", detail)
    return StarlinkError("error", detail)
