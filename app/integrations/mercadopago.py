import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

MP_API_BASE = "https://api.mercadopago.com"


@dataclass
class PaymentPreference:
    init_point: str
    preference_id: str


class MercadoPagoClient:
    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    async def create_payment_link(
        self,
        title: str,
        unit_price: float,
        quantity: int = 1,
        currency: str = "ARS",
    ) -> PaymentPreference:
        payload = {
            "items": [
                {
                    "title": title,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "currency_id": currency,
                }
            ]
        }

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

        if response.status_code != 201:
            logger.error(
                "MercadoPago API error [status=%d body=%s]",
                response.status_code,
                response.text,
            )
            raise MercadoPagoError(
                f"No se pudo generar el link de pago (status {response.status_code})"
            )

        data = response.json()
        return PaymentPreference(
            init_point=data["init_point"],
            preference_id=data["id"],
        )


class MercadoPagoError(Exception):
    pass