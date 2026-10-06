import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from opentelemetry import trace, metrics, _logs
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.resources import Resource

# Setup OpenTelemetry resources and exporters
resource = Resource.create({
    "service.name": "order-tracker",
})

otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

# Traces setup
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
if otlp_endpoint:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
    )
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("order-tracker")

# Metrics setup
metric_readers = [
    PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=1000)
]
if otlp_endpoint:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    metric_readers.append(
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics"),
            export_interval_millis=1000,
        )
    )
meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("order-tracker")
request_counter = meter.create_counter(
    "http_requests_total",
    description="Total number of HTTP requests",
    unit="1",
)

# Logs setup
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogRecordExporter()))
if otlp_endpoint:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(OTLPLogExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/logs"))
    )
_logs.set_logger_provider(logger_provider)

otel_logging_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
logger = logging.getLogger("order-tracker")
logger.setLevel(logging.INFO)
logger.addHandler(otel_logging_handler)


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}



def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield
    meter_provider.shutdown()
    tracer_provider.shutdown()
    logger_provider.shutdown()



app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    status_code = 200
    with tracer.start_as_current_span("list_orders") as span:
        span.set_attribute("http.route", "/api/orders")
        logger.info("Listing orders")
        try:
            with connect() as db:
                rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
            return [as_dict(row) for row in rows]
        except Exception as exc:
            status_code = 500
            span.record_exception(exc)
            span.set_attribute("http.status_code", status_code)
            logger.error(f"Error listing orders: {exc}")
            raise
        finally:
            span.set_attribute("http.status_code", status_code)
            request_counter.add(1, {
                "route": "/api/orders",
                "status_code": status_code,
            })
            meter_provider.force_flush()
            tracer_provider.force_flush()
            logger_provider.force_flush()




@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    status_code = 200
    with tracer.start_as_current_span("get_order") as span:
        span.set_attribute("http.route", "/api/orders/{order_id}")
        span.set_attribute("order.id", order_id)
        logger.info(f"Looking up order {order_id}")
        try:
            with connect() as db:
                row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                status_code = 404
                logger.warning(f"Order not found: {order_id}")
                raise HTTPException(404, "Order not found")
            return order_detail(row)
        except HTTPException as he:
            status_code = he.status_code
            span.set_attribute("http.status_code", status_code)
            raise
        except Exception as exc:
            status_code = 500
            span.record_exception(exc)
            span.set_attribute("http.status_code", status_code)
            logger.error(f"Error looking up order {order_id}: {exc}")
            raise
        finally:
            span.set_attribute("http.status_code", status_code)
            request_counter.add(1, {
                "route": "/api/orders/{order_id}",
                "status_code": status_code,
            })
            meter_provider.force_flush()
            tracer_provider.force_flush()
            logger_provider.force_flush()




@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
