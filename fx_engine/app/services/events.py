"""
RabbitMQ event publisher — async, fire-and-forget.

Events are published to a durable topic exchange (fx.events) after the
database transaction commits.  Publishing never blocks or raises inside
the FX engine path — if RabbitMQ is unavailable the event is logged and
dropped.

Routing keys
  quote.created   — new quote generated
  quote.executed  — quote atomically executed, balances updated
  quote.expired   — quote TTL elapsed before execution
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from app.config import settings

log = logging.getLogger(__name__)

EXCHANGE_NAME = "fx.events"

_connection: Optional[aio_pika.RobustConnection] = None
_channel: Optional[aio_pika.Channel] = None
_exchange: Optional[aio_pika.Exchange] = None


async def connect_events() -> None:
    """Open connection and declare the exchange. Called at startup."""
    global _connection, _channel, _exchange
    try:
        _connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        _channel = await _connection.channel()
        _exchange = await _channel.declare_exchange(
            EXCHANGE_NAME,
            ExchangeType.TOPIC,
            durable=True,
        )
        log.info("RabbitMQ connected, exchange '%s' ready", EXCHANGE_NAME)
    except Exception as exc:
        log.warning("RabbitMQ unavailable at startup — events will be dropped: %s", exc)


async def close_events() -> None:
    """Close the RabbitMQ connection gracefully. Called at shutdown."""
    global _connection
    if _connection and not _connection.is_closed:
        await _connection.close()


async def publish(routing_key: str, payload: dict) -> None:
    """
    Publish a JSON event to the fx.events exchange.

    Silently drops the event if the broker is unavailable — the FX
    operation has already committed and must not be rolled back.
    """
    if _exchange is None:
        return
    try:
        body = json.dumps(
            {
                "event": routing_key,
                "published_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
        ).encode()
        message = Message(
            body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
        )
        await _exchange.publish(message, routing_key=routing_key)
    except Exception as exc:
        log.warning("Event publish failed [%s]: %s", routing_key, exc)
