import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_seeded_express_order_lookup_at_month_boundary(client):
    """
    Regression test for the Homework 4 production incident:
    The seeded order 'express-1002' was created at previous month-end.
    Looking it up must return 200 with delivery correctly rolled into the next month.
    """
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    order = response.json()
    assert order["id"] == "express-1002"
    assert order["priority"] == "express"
    assert "estimated_delivery" in order
    assert order["estimated_delivery"] == "2026-10-02"


@pytest.mark.parametrize(
    "created_at_iso, expected_delivery",
    [
        ("2026-01-31T12:00:00+00:00", "2026-02-02"),  # 31-day month end (day+2 would be 33)
        ("2026-04-30T12:00:00+00:00", "2026-05-02"),  # 30-day month end (day+2 would be 32)
        ("2026-02-28T12:00:00+00:00", "2026-03-02"),  # Non-leap February end
        ("2024-02-28T12:00:00+00:00", "2024-03-01"),  # Leap February 28 -> Feb 29 + 1 = Mar 1
        ("2024-02-29T12:00:00+00:00", "2024-03-02"),  # Leap February 29 -> Mar 2
        ("2026-12-31T12:00:00+00:00", "2027-01-02"),  # Year boundary rollover
    ],
)
def test_express_order_delivery_calculation_across_month_and_year_boundaries(client, created_at_iso, expected_delivery):
    """
    Regression test: ensures timedelta arithmetic safely handles all calendar month
    and year boundaries without raising ValueError (day is out of range for month).
    """
    import uuid
    order_id = f"test-express-{uuid.uuid4().hex[:8]}"
    with main.connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, "TestCustomer", "TestItem", "express", "preparing", created_at_iso),
        )

    response = client.get(f"/api/orders/{order_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["estimated_delivery"] == expected_delivery
