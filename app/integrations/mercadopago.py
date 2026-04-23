import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

MP_API_BASE = "https://api.mercadopago.com"


@dataclass
class PaymentPreference:
    init_point: str
    preference_id: str


@dataclass
class PreferenceDetails:
    preference_id: str
    items: list
    total: float


class MercadoPagoClient:
    def __init__(self, access_token: str, sandbox: bool = True) -> None:
        self._access_token = access_token
        self._sandbox = sandbox
        logger.info("MP token prefix: %s", self._access_token[:10] if self._access_token else "NONE")

    async def create_payment_link(
        self,
        items: list[dict],
        currency: str = "ARS",
    ) -> PaymentPreference:
        """
        Create a MercadoPago preference with one or more items.

        Each item in `items` must have:
            - title (str)
            - quantity (int)
            - unit_price (float)
        """
        mp_items = [
            {
                "title": item["title"],
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "currency_id": currency,
            }
            for item in items
        ]
        payload = {"items": mp_items}

        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{MP_API_BASE}/checkout/preferences",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self._access_token}",
                            "Content-Type": "application/json",
                        },
                        timeout=10.0,
                    )
            except httpx.TimeoutException as e:
                logger.warning("MercadoPago request timed out (attempt %d/3)", attempt + 1)
                last_exc = MercadoPagoError("Timeout al conectar con MercadoPago")
                continue
            except httpx.RequestError as e:
                logger.warning("MercadoPago network error (attempt %d/3): %s", attempt + 1, e)
                last_exc = MercadoPagoError(f"Error de red al conectar con MercadoPago: {e}")
                continue

            if response.status_code == 201:
                break
            if response.status_code >= 500:
                logger.warning(
                    "MercadoPago 5xx (attempt %d/3) [status=%d]", attempt + 1, response.status_code
                )
                last_exc = MercadoPagoError(
                    f"No se pudo generar el link de pago (status {response.status_code})"
                )
                continue
            # 4xx — not retryable
            logger.error(
                "MercadoPago API error [status=%d body=%s]",
                response.status_code,
                response.text,
            )
            raise MercadoPagoError(
                f"No se pudo generar el link de pago (status {response.status_code})"
            )
        else:
            logger.error("MercadoPago create_payment_link failed after 3 attempts")
            raise last_exc  # type: ignore[misc]

        data = response.json()
        key = "sandbox_init_point" if self._sandbox else "init_point"
        return PaymentPreference(
            init_point=data.get(key) or data["init_point"],
            preference_id=data["id"],
        )
        
    async def get_preference(self, preference_id: str) -> PreferenceDetails:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{MP_API_BASE}/checkout/preferences/{preference_id}",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    timeout=10.0,
                )
        except httpx.TimeoutException:
            logger.error("MercadoPago get_preference timed out")
            raise MercadoPagoError("Timeout al conectar con MercadoPago")
        except httpx.RequestError as e:
            logger.error("MercadoPago get_preference network error: %s", e)
            raise MercadoPagoError(f"Error de red al conectar con MercadoPago: {e}")
 
        if response.status_code != 200:
            logger.error(
                "MercadoPago get_preference error [status=%d body=%s]",
                response.status_code,
                response.text,
            )
            raise MercadoPagoError(
                f"No se pudo obtener la preferencia (status {response.status_code})"
            )
 
        data = response.json()
        raw_items = data.get("items", [])
        total = sum(i["unit_price"] * i["quantity"] for i in raw_items)

        return PreferenceDetails(
            preference_id=preference_id,
            items=raw_items,
            total=total,
        )


class MercadoPagoError(Exception):
    pass