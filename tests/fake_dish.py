"""A stand-in Starlink dish: SpaceX.API.Device.Device/Handle with reflection.

Message definitions are built in code (no protoc) and served over gRPC
reflection, so the client under test learns them exactly the way it does
from a real dish. Field names follow the real API; the set is a subset, and
the field numbers are ours (the client never relies on them).
"""

from __future__ import annotations

from concurrent import futures

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from grpc_reflection.v1alpha import reflection

T = descriptor_pb2.FieldDescriptorProto
PACKAGE = "SpaceX.API.Device"


def _message(fd, name, fields, oneof=None):
    m = fd.message_type.add(name=name)
    if oneof:
        m.oneof_decl.add(name=oneof)
    for i, (fname, ftype, *rest) in enumerate(fields, start=1):
        f = m.field.add(name=fname, number=i, label=T.LABEL_OPTIONAL)
        if isinstance(ftype, str):  # message or enum reference
            f.type = T.TYPE_ENUM if ftype.startswith("enum:") else T.TYPE_MESSAGE
            f.type_name = f".{PACKAGE}.{ftype.removeprefix('enum:')}"
        else:
            f.type = ftype
        if rest and rest[0] == "repeated":
            f.label = T.LABEL_REPEATED
        if oneof:
            f.oneof_index = 0
    return m


def _enum(fd, name, values):
    e = fd.enum_type.add(name=name)
    for i, v in enumerate(values):
        e.value.add(name=v, number=i)


def build_pool():
    fd = descriptor_pb2.FileDescriptorProto(name="spacex/api/device/device.proto", package=PACKAGE, syntax="proto3")
    _enum(fd, "BandwidthRestrictedReason", ["UNKNOWN_RESTRICTED", "NO_LIMIT", "OVERAGE_LIMIT", "POLICY_LIMIT"])
    _enum(fd, "DisablementCode", ["UNKNOWN_STATE", "OKAY", "NO_ACTIVE_ACCOUNT", "TOO_FAR_FROM_SERVICE_ADDRESS"])

    _message(fd, "GetStatusRequest", [])
    _message(fd, "GetLocationRequest", [("source", T.TYPE_STRING)])
    _message(fd, "DeviceInfo", [("id", T.TYPE_STRING), ("hardware_version", T.TYPE_STRING),
                                ("software_version", T.TYPE_STRING)])
    _message(fd, "DeviceState", [("uptime_s", T.TYPE_UINT64)])
    _message(fd, "ObstructionStats", [("fraction_obstructed", T.TYPE_FLOAT),
                                      ("currently_obstructed", T.TYPE_BOOL)])
    _message(fd, "Alerts", [("motors_stuck", T.TYPE_BOOL), ("thermal_throttle", T.TYPE_BOOL),
                            ("slow_ethernet_speeds", T.TYPE_BOOL)])
    _message(fd, "GpsStats", [("gps_valid", T.TYPE_BOOL), ("gps_sats", T.TYPE_UINT32)])
    _message(fd, "DishGetStatusResponse", [
        ("device_info", "DeviceInfo"), ("device_state", "DeviceState"),
        ("pop_ping_latency_ms", T.TYPE_FLOAT), ("downlink_throughput_bps", T.TYPE_FLOAT),
        ("uplink_throughput_bps", T.TYPE_FLOAT), ("obstruction_stats", "ObstructionStats"),
        ("alerts", "Alerts"), ("is_snr_above_noise_floor", T.TYPE_BOOL),
        ("disablement_code", "enum:DisablementCode"),
        ("dl_bandwidth_restricted_reason", "enum:BandwidthRestrictedReason"),
        ("ul_bandwidth_restricted_reason", "enum:BandwidthRestrictedReason"),
        ("gps_stats", "GpsStats"),
    ])
    _message(fd, "LLA", [("lat", T.TYPE_DOUBLE), ("lon", T.TYPE_DOUBLE), ("alt", T.TYPE_DOUBLE)])
    _message(fd, "GetLocationResponse", [("lla", "LLA"), ("source", T.TYPE_STRING)])
    _message(fd, "Request", [("get_status", "GetStatusRequest"), ("get_location", "GetLocationRequest")],
             oneof="request")
    resp = _message(fd, "Response", [("dish_get_status", "DishGetStatusResponse"),
                                     ("get_location", "GetLocationResponse")], oneof="response")
    resp.field.add(name="api_version", number=99, label=T.LABEL_OPTIONAL, type=T.TYPE_UINT64)

    svc = fd.service.add(name="Device")
    svc.method.add(name="Handle", input_type=f".{PACKAGE}.Request", output_type=f".{PACKAGE}.Response")

    pool = descriptor_pool.DescriptorPool()
    pool.Add(fd)
    return pool


class FakeDish:
    """Serve a canned getStatus; optionally refuse getLocation like a dish
    with local location access switched off."""

    def __init__(self, status: dict, location: dict | None = None, reflection_enabled=True, port=0):
        self.status = status
        self.location = location
        self.calls = []
        pool = build_pool()
        self.Request = message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{PACKAGE}.Request"))
        self.Response = message_factory.GetMessageClass(pool.FindMessageTypeByName(f"{PACKAGE}.Response"))

        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        handler = grpc.method_handlers_generic_handler(f"{PACKAGE}.Device", {
            "Handle": grpc.unary_unary_rpc_method_handler(
                self._handle, request_deserializer=self.Request.FromString,
                response_serializer=self.Response.SerializeToString),
        })
        self.server.add_generic_rpc_handlers((handler,))
        if reflection_enabled:
            reflection.enable_server_reflection(
                (f"{PACKAGE}.Device", reflection.SERVICE_NAME), self.server, pool=pool)
        self.port = self.server.add_insecure_port(f"127.0.0.1:{port}")

    @property
    def address(self):
        return f"127.0.0.1:{self.port}"

    def __enter__(self):
        self.server.start()
        return self

    def __exit__(self, *exc):
        self.server.stop(None)

    def _handle(self, request, context):
        kind = request.WhichOneof("request")
        self.calls.append(kind)
        if kind == "get_status":
            return json_format.ParseDict({"apiVersion": "43", "dishGetStatus": self.status}, self.Response())
        if kind == "get_location":
            if self.location is None:
                context.abort(grpc.StatusCode.PERMISSION_DENIED,
                              "Failed to get location: Requests are not enabled on this device")
            return json_format.ParseDict({"apiVersion": "43", "getLocation": self.location}, self.Response())
        context.abort(grpc.StatusCode.UNIMPLEMENTED, f"unsupported: {kind}")
