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
    title: str
    quantity: int
    unit_price: float
    total: float


class MercadoPagoClient:
    def __init__(self, access_token: str, sandbox: bool = True) -> None:
        self._access_token = access_token
        self._sandbox = sandbox
        logger.info("MP token prefix: %s", self._access_token[:10] if self._access_token else "NONE")

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
        except httpx.TimeoutException:
            logger.error("MercadoPago request timed out")
            raise MercadoPagoError("Timeout al conectar con MercadoPago")
        except httpx.RequestError as e:
            logger.error("MercadoPago network error: %s", e)
            raise MercadoPagoError(f"Error de red al conectar con MercadoPago: {e}")

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
        item = data["items"][0]
        quantity = item["quantity"]
        unit_price = item["unit_price"]
 
        return PreferenceDetails(
            preference_id=preference_id,
            title=item["title"],
            quantity=quantity,
            unit_price=unit_price,
            total=unit_price * quantity,
        )


class MercadoPagoError(Exception):
    pass