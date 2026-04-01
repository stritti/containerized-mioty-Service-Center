"""
telemetry.py – OpenTelemetry SDK initialisation for the BSSCI Service Center.

This module is only activated when the environment variable ``OTEL_ENABLED``
is set to ``true``.  All imports from the opentelemetry packages are kept
*inside* the functions so that the application continues to start without
errors when the packages are not installed (i.e. when OTEL_ENABLED=false).

Exported helpers
----------------
setup_telemetry()
    Call once at startup.  Configures the TracerProvider, MeterProvider, and
    a LoggingHandler that bridges Python :mod:`logging` records into OTLP.
    Returns a dict with the top-level tracer and meter objects (or no-op
    objects when telemetry is disabled / packages not installed).

get_tracer(name)
    Returns an OpenTelemetry Tracer (or a no-op stub).

get_meter(name)
    Returns an OpenTelemetry Meter (or a no-op stub).

record_uplink(sensor_eui, bs_eui, snr, rssi, payload_bytes)
    Convenience wrapper: increments the uplink counter metric and emits a
    trace span with the relevant attributes.

record_bs_connection(bs_eui, event)
    Convenience wrapper: emits a gauge-style event for base station
    connectivity changes.  *event* is one of "connected", "disconnected".

Instrument names / span names
------------------------------
All instruments follow the ``bssci.`` prefix so they are easy to filter in
Prometheus / Jaeger / Grafana:

Counters
  bssci.sensor.uplinks_total          – uplink messages forwarded to MQTT
  bssci.sensor.duplicates_total       – deduplicated / dropped messages
  bssci.sensor.attach_requests_total  – attach requests sent to a BS
  bssci.sensor.detach_requests_total  – detach requests sent to a BS
  bssci.mqtt.messages_published_total – MQTT messages placed on out-queue
  bssci.mqtt.messages_received_total  – MQTT messages received from broker
  bssci.mqtt.connection_errors_total  – MQTT connection/reconnection errors

UpDownCounters
  bssci.bs.connected_count     – currently connected base stations

Histograms
  bssci.sensor.snr_db          – Signal-to-Noise Ratio per uplink (dB)
  bssci.sensor.rssi_dbm        – RSSI per uplink (dBm)

Span names (traces)
  bssci.bs.handle_client       – lifecycle of a single BS TCP connection
  bssci.sensor.attach          – sensor attach request/response cycle
  bssci.sensor.detach          – sensor detach request
  bssci.sensor.uplink          – individual uplink message processing
  bssci.mqtt.publish           – MQTT message publication
  bssci.mqtt.connect           – MQTT broker connection attempt
"""

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_OTEL_ENABLED: bool = os.getenv("OTEL_ENABLED", "false").lower() == "true"

# Module-level singletons set by setup_telemetry()
_tracer: Any = None
_meter: Any = None
_counters: Dict[str, Any] = {}
_histograms: Dict[str, Any] = {}
_updown: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# No-op stubs used when telemetry is disabled or packages are missing
# ---------------------------------------------------------------------------

class _NoOpSpan:
    """Minimal no-op span used when tracing is disabled."""
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def set_attribute(self, *_):
        pass
    def record_exception(self, *_):
        pass
    def set_status(self, *_):
        pass


class _NoOpTracer:
    def start_as_current_span(self, *_, **__):
        return _NoOpSpan()
    def start_span(self, *_, **__):
        return _NoOpSpan()


class _NoOpMeter:
    def create_counter(self, *_, **__):
        return _NoOpInstrument()
    def create_histogram(self, *_, **__):
        return _NoOpInstrument()
    def create_up_down_counter(self, *_, **__):
        return _NoOpInstrument()
    def create_observable_gauge(self, *_, **__):
        return _NoOpInstrument()


class _NoOpInstrument:
    def add(self, *_, **__):
        pass
    def record(self, *_, **__):
        pass


# ---------------------------------------------------------------------------
# Public setup
# ---------------------------------------------------------------------------

def setup_telemetry() -> Dict[str, Any]:
    """Initialise OpenTelemetry SDK.

    Returns a dict with keys ``tracer`` and ``meter``.
    When telemetry is disabled or the SDK packages are not installed the
    returned objects are no-op stubs so callers need no guard logic.
    """
    global _tracer, _meter, _counters, _histograms, _updown

    if not _OTEL_ENABLED:
        logger.info("OpenTelemetry disabled (OTEL_ENABLED != true) – using no-op stubs")
        _tracer = _NoOpTracer()
        _meter = _NoOpMeter()
        return {"tracer": _tracer, "meter": _meter}

    try:
        _setup_tracing()
        _setup_metrics()
        _setup_log_bridge()
        _register_instruments()
        logger.info(
            "✅ OpenTelemetry initialized – traces: %s, metrics: %s, logs: %s",
            _otlp_endpoint("v1/traces"),
            _otlp_endpoint("v1/metrics"),
            _otlp_endpoint("v1/logs"),
        )
    except ImportError as exc:
        logger.warning(
            "⚠️  OpenTelemetry packages not installed – falling back to no-op stubs. "
            "Install them with: pip install opentelemetry-sdk "
            "opentelemetry-exporter-otlp-proto-http opentelemetry-instrumentation-logging. "
            "Original error: %s", exc
        )
        _tracer = _NoOpTracer()
        _meter = _NoOpMeter()
    except Exception as exc:  # pragma: no cover
        logger.error("❌ OpenTelemetry initialisation failed: %s", exc)
        _tracer = _NoOpTracer()
        _meter = _NoOpMeter()

    return {"tracer": _tracer, "meter": _meter}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _otlp_endpoint(signal_path: str) -> str:
    """Build the full OTLP HTTP endpoint URL for a specific signal.

    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is the *base* URL (no trailing slash, no
    path suffix).  When ``endpoint=`` is passed *directly* to an OTLP exporter
    constructor the SDK does **not** append the signal-specific sub-path – that
    auto-appending only occurs when the exporter reads the env-var itself.
    This helper therefore appends the correct path so that the collector
    receives requests at the right endpoint in every deployment, including
    Docker where the service is reachable as ``otel-collector``.

    Examples
    --------
    base = "http://otel-collector:4318"  →  "http://otel-collector:4318/v1/traces"
    base = "http://otel-collector:4318/" →  "http://otel-collector:4318/v1/traces"
    """
    base = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
    ).rstrip("/")
    return f"{base}/{signal_path.lstrip('/')}"


def _setup_tracing() -> None:
    global _tracer
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "bssci-service-center"),
        "service.version": _read_version(),
    })
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=_otlp_endpoint("v1/traces")
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(__name__)


def _setup_metrics() -> None:
    global _meter
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "bssci-service-center"),
    })
    exporter = OTLPMetricExporter(
        endpoint=_otlp_endpoint("v1/metrics")
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15_000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    _meter = metrics.get_meter(__name__)


def _setup_log_bridge() -> None:
    """Bridge Python logging records to OTLP via the OpenTelemetry Logs SDK."""
    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", "bssci-service-center"),
        })
        log_provider = LoggerProvider(resource=resource)
        log_exporter = OTLPLogExporter(
            endpoint=_otlp_endpoint("v1/logs")
        )
        log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
        # Attach to the root logger so every logger in the application exports records
        logging.getLogger().addHandler(handler)
        logger.info("✅ OpenTelemetry log bridge active – Python log records forwarded via OTLP")
    except ImportError:
        logger.debug("opentelemetry-sdk-logs not available; log bridge skipped")


def _register_instruments() -> None:
    global _counters, _histograms, _updown
    if _meter is None or isinstance(_meter, _NoOpMeter):
        return

    _counters["uplinks"] = _meter.create_counter(
        "bssci.sensor.uplinks_total",
        unit="1",
        description="Total uplink messages forwarded to MQTT",
    )
    _counters["duplicates"] = _meter.create_counter(
        "bssci.sensor.duplicates_total",
        unit="1",
        description="Uplink messages filtered by deduplication",
    )
    _counters["attach_requests"] = _meter.create_counter(
        "bssci.sensor.attach_requests_total",
        unit="1",
        description="Attach requests sent to base stations",
    )
    _counters["detach_requests"] = _meter.create_counter(
        "bssci.sensor.detach_requests_total",
        unit="1",
        description="Detach requests sent to base stations",
    )
    _counters["mqtt_published"] = _meter.create_counter(
        "bssci.mqtt.messages_published_total",
        unit="1",
        description="MQTT messages placed on the outgoing queue",
    )
    _counters["mqtt_received"] = _meter.create_counter(
        "bssci.mqtt.messages_received_total",
        unit="1",
        description="MQTT messages received from the broker",
    )
    _counters["mqtt_errors"] = _meter.create_counter(
        "bssci.mqtt.connection_errors_total",
        unit="1",
        description="MQTT connection or reconnection errors",
    )
    _updown["bs_connected"] = _meter.create_up_down_counter(
        "bssci.bs.connected_count",
        unit="1",
        description="Number of currently connected base stations",
    )
    _histograms["snr"] = _meter.create_histogram(
        "bssci.sensor.snr_db",
        unit="dB",
        description="Signal-to-Noise Ratio per uplink message",
    )
    _histograms["rssi"] = _meter.create_histogram(
        "bssci.sensor.rssi_dbm",
        unit="dBm",
        description="RSSI per uplink message",
    )


def _read_version() -> str:
    try:
        with open("VERSION") as fh:
            return fh.read().strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

def get_tracer(name: str = __name__) -> Any:
    """Return a tracer for the given *name* (or a no-op stub if telemetry is disabled)."""
    if not _OTEL_ENABLED or _tracer is None:
        return _NoOpTracer()
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except Exception:
        return _NoOpTracer()


def get_meter(name: str = __name__) -> Any:
    """Return a meter for the given *name* (or a no-op stub if telemetry is disabled)."""
    if not _OTEL_ENABLED or _meter is None:
        return _NoOpMeter()
    try:
        from opentelemetry import metrics
        return metrics.get_meter(name)
    except Exception:
        return _NoOpMeter()


# ---------------------------------------------------------------------------
# Convenience wrappers called from TLSServer / MQTTClient
# ---------------------------------------------------------------------------

def record_uplink(sensor_eui: str, bs_eui: str, snr: float, rssi: float,
                  payload_bytes: int = 0) -> None:
    """Increment uplink counter and SNR/RSSI histograms."""
    attrs = {"sensor_eui": sensor_eui, "bs_eui": bs_eui}
    if "uplinks" in _counters:
        _counters["uplinks"].add(1, attrs)
    if "snr" in _histograms:
        _histograms["snr"].record(snr, attrs)
    if "rssi" in _histograms:
        _histograms["rssi"].record(rssi, attrs)


def record_duplicate(sensor_eui: str, bs_eui: str) -> None:
    """Increment the deduplication drop counter."""
    if "duplicates" in _counters:
        _counters["duplicates"].add(1, {"sensor_eui": sensor_eui, "bs_eui": bs_eui})


def record_attach(sensor_eui: str, bs_eui: str) -> None:
    """Increment the attach request counter."""
    if "attach_requests" in _counters:
        _counters["attach_requests"].add(1, {"sensor_eui": sensor_eui, "bs_eui": bs_eui})


def record_detach(sensor_eui: str, bs_eui: str) -> None:
    """Increment the detach request counter."""
    if "detach_requests" in _counters:
        _counters["detach_requests"].add(1, {"sensor_eui": sensor_eui, "bs_eui": bs_eui})


def record_mqtt_published(topic: str) -> None:
    """Increment the MQTT published counter."""
    if "mqtt_published" in _counters:
        _counters["mqtt_published"].add(1, {"topic": topic})


def record_mqtt_received(topic: str) -> None:
    """Increment the MQTT received counter."""
    if "mqtt_received" in _counters:
        _counters["mqtt_received"].add(1, {"topic": topic})


def record_mqtt_error() -> None:
    """Increment the MQTT connection error counter."""
    if "mqtt_errors" in _counters:
        _counters["mqtt_errors"].add(1)


def record_bs_connection(bs_eui: str, event: str) -> None:
    """Track base station connected/disconnected events.

    *event* should be ``"connected"`` or ``"disconnected"``.
    """
    if "bs_connected" in _updown:
        delta = 1 if event == "connected" else -1
        _updown["bs_connected"].add(delta, {"bs_eui": bs_eui})
