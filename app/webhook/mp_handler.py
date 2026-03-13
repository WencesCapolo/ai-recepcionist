import json
import logging

import httpx
from upstash_redis import Redis

from app.clients.service import ClientService
from app.integrations.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

MP_API_BASE = "https://api.mercadopago.com"


async def handle_mp_notification(
    body: dict,
    redis: Redis,
    client_service: ClientService,
    whatsapp: WhatsAppClient,
) -> None:
    # MP sends different notification types — we only care about payments
    topic = body.get("topic") or body.get("type")
    if topic not in ("payment", "merchant_order"):
        logger.info("Ignoring MP notification type=%s", topic)
        return

    # Extract payment or merchant_order ID
    resource_id = _extract_resource_id(body)
    if not resource_id:
        logger.warning("MP notification missing resource ID: %s", body)
        return

    # Dedup — MP sends duplicates
    dedup_key = f"mp_notif_dedup:{resource_id}"
    acquired = redis.set(dedup_key, "1", nx=True, ex=60)
    if not acquired:
        logger.info("Duplicate MP notification for resource=%s, dropping", resource_id)
        return

    logger.info("Processing MP notification topic=%s resource=%s", topic, resource_id)

    # Fetch payment details from MP to confirm status and get preference_id
    if topic == "payment":
        await _handle_payment(resource_id, redis, client_service, whatsapp)
    elif topic == "merchant_order":
        await _handle_merchant_order(resource_id, redis, client_service, whatsapp)


async def _handle_payment(
    payment_id: str,
    redis: Redis,
    client_service: ClientService,
    whatsapp: WhatsAppClient,
) -> None:
    # Look up payment in MP to get status + preference_id
    payment = await _fetch_mp_resource(f"/v1/payments/{payment_id}", client_service)
    if not payment:
        return

    status = payment.get("status")
    if status != "approved":
        logger.info("Payment %s status=%s — not approved, skipping", payment_id, status)
        return

    preference_id = payment.get("order", {}).get("id") or payment.get("preference_id")
    if not preference_id:
        logger.warning("Payment %s has no preference_id", payment_id)
        return

    await _send_confirmation(
        preference_id=str(preference_id),
        redis=redis,
        whatsapp=whatsapp,
        client_service=client_service,
    )


async def _handle_merchant_order(
    order_id: str,
    redis: Redis,
    client_service: ClientService,
    whatsapp: WhatsAppClient,
) -> None:
    order = await _fetch_mp_resource(f"/merchant_orders/{order_id}", client_service)
    if not order:
        return

    # Only act if order is fully paid
    if order.get("order_status") != "paid":
        logger.info("Merchant order %s status=%s — not paid, skipping", order_id, order.get("order_status"))
        return

    preference_id = order.get("preference_id")
    if not preference_id:
        logger.warning("Merchant order %s has no preference_id", order_id)
        return

    await _send_confirmation(
        preference_id=str(preference_id),
        redis=redis,
        whatsapp=whatsapp,
        client_service=client_service,
    )


async def _send_confirmation(
    preference_id: str,
    redis: Redis,
    whatsapp: WhatsAppClient,
    client_service: ClientService,
) -> None:
    # Look up payment metadata stored when the link was created
    redis_key = f"mp_payment:{preference_id}"
    raw = redis.get(redis_key)

    if not raw:
        logger.warning(
            "No payment metadata found for preference_id=%s — may have expired or never stored",
            preference_id,
        )
        return

    meta = json.loads(raw)
    user_phone = meta["user_phone"]
    product = meta["product"]
    quantity = meta["quantity"]
    total = meta["total"]

    # Fetch client config to get the business name + WhatsApp number
    client_config = await client_service.get_client_by_id(meta["client_id"])
    if not client_config:
        logger.error("Client %s not found for MP confirmation", meta["client_id"])
        return

    message = (
        f"Pago confirmado! ✅\n"
        f"Producto: {quantity}x {product}\n"
        f"Total pagado: ${total:,.0f} ARS\n"
        f"Ya podés pasar por el local a retirar tu pedido en el horario de atención."
    )

    await whatsapp.send_message(to=user_phone, text=message)
    logger.info(
        "Payment confirmation sent [preference=%s phone=%s product=%s total=%.2f]",
        preference_id, user_phone, product, total,
    )

    # Clean up Redis key — no longer needed
    redis.delete(redis_key)


async def _fetch_mp_resource(path: str, client_service: ClientService) -> dict | None:
    # We need the MP token — get it from any active client that has one
    # (in multi-tenant: you'd look up by collector_id from the notification)
    token = await client_service.get_any_mp_token()
    if not token:
        logger.error("No MP access token available to fetch %s", path)
        return None

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{MP_API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.error("MP API error fetching %s [status=%d]", path, response.status_code)
        return None

    return response.json()


def _extract_resource_id(body: dict) -> str | None:
    # MP sends either body.data.id or body.resource as a URL
    if "data" in body and "id" in body.get("data", {}):
        return str(body["data"]["id"])
    resource = body.get("resource", "")
    if resource:
        # resource is a URL like https://api.mp.com/v1/payments/123456
        return resource.rstrip("/").split("/")[-1]
    return None