"""Starts the real app (uvicorn subprocess) exporting OTLP/gRPC to a local receiver and checks the spans
that arrive over the wire. This verifies the exporter wiring; it does not replace the Collector/Jaeger check
in scripts/verify.sh."""
import os
import socket
import subprocess
import sys
import time
from concurrent import futures

import grpc
import httpx
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc


class Receiver(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self):
        self.spans, self.resources = [], []

    def Export(self, request, context):
        for rs in request.resource_spans:
            self.resources.append({a.key: a.value.string_value for a in rs.resource.attributes})
            for ss in rs.scope_spans:
                self.spans.extend(ss.spans)
        return trace_service_pb2.ExportTraceServiceResponse()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_spans_reach_otlp_receiver():
    recv = Receiver()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(recv, server)
    otlp_port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    app_port = free_port()
    env = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": f"127.0.0.1:{otlp_port}",
           "TRACEGUARD_ENV": "local", "TRACEGUARD_ENABLE_FAULT_INJECTION": "true"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(app_port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{app_port}"
        for _ in range(50):
            try:
                if httpx.get(f"{base}/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)
        body = httpx.post(f"{base}/recommend", json={"user_id": "u", "category": "books", "budget": 100}).json()
        deadline = time.time() + 15  # BatchSpanProcessor flushes every ~5s
        while time.time() < deadline and not recv.spans:
            time.sleep(0.25)
        assert recv.spans, "no spans received by OTLP receiver"
        names = {s.name for s in recv.spans}
        assert {"recommend.validate_input", "recommend.lookup_catalog",
                "recommend.validate_business_rules"} <= names
        assert {s.trace_id.hex() for s in recv.spans} == {body["trace_id"]}
        assert recv.resources[0]["service.name"] == "traceguard"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server.stop(0)
