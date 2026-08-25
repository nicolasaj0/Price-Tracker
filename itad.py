"""Módulo de Integração com a API do IsThereAnyDeal (ITAD v2).
Obtenção do Menor Preço Histórico Real (All-Time Low - ATL) para Steam com cache TTL e fallback gracioso.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from dotenv import load_dotenv
import httpx

logger = logging.getLogger("PriceTracker.ITAD")

load_dotenv()
ITAD_API_KEY = os.getenv("ITAD_API_KEY", "").strip()

ITAD_LOOKUP_URL = "https://api.isthereanydeal.com/games/lookup/v1"
ITAD_OVERVIEW_URL = "https://api.isthereanydeal.com/games/overview/v2"

STEAM_SHOP_ID = 61  # ID oficial da loja Steam no IsThereAnyDeal

_global_itad_client: Optional[httpx.AsyncClient] = None


class InMemoryTTLCache:
    """Cache em memória LRU com expiração TTL (Zero dependências externas, < 500 KB RAM)."""

    def __init__(self, maxsize: int = 512, ttl_seconds: float = 21600.0):  # 6 horas de TTL padrão
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._cache: OrderedDict[str, Tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None
        expire_at, value = self._cache[key]
        if time.monotonic() > expire_at:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self.maxsize:
            self._cache.popitem(last=False)
        self._cache[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        self._cache.clear()


_itad_cache = InMemoryTTLCache(maxsize=512, ttl_seconds=21600.0)


async def get_http_client() -> httpx.AsyncClient:
    """Obtém ou inicializa a sessão HTTP compartilhada para o ITAD."""
    global _global_itad_client
    if _global_itad_client is None or _global_itad_client.is_closed:
        _global_itad_client = httpx.AsyncClient(
            headers={"User-Agent": "PriceTracker-DiscordBot/1.1"},
            timeout=3.0,
        )
    return _global_itad_client


async def close_http_client() -> None:
    """Encerra a sessão HTTP do ITAD."""
    global _global_itad_client
    if _global_itad_client and not _global_itad_client.is_closed:
        await _global_itad_client.aclose()
        _global_itad_client = None


def format_itad_date(date_str: Optional[str]) -> str:
    """Formata timestamp ISO do ITAD para exibição amigável (DD/MM/AAAA)."""
    if not date_str:
        return ""
    try:
        clean_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return ""


async def get_steam_all_time_low(
    appid: str,
    country_code: str = "BR",
    api_key: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """Consulta o Menor Preço Histórico Real (All-Time Low) da Steam no IsThereAnyDeal.

    Retorna None caso a chave não esteja configurada, ocorra timeout ou falha na API.
    """
    key = (api_key if api_key is not None else os.getenv("ITAD_API_KEY", "")).strip()
    if not key:
        return None

    appid_str = str(appid).strip()
    if not appid_str or not appid_str.isdigit():
        return None

    cc = country_code.upper().strip()
    cache_key = f"itad_{appid_str}_{cc}"
    cached = _itad_cache.get(cache_key)
    if cached is not None:
        return cached

    cli = client or await get_http_client()

    try:
        # Passo 1: Lookup para obter o UUID do jogo no ITAD
        lookup_params = {
            "key": key,
            "appid": appid_str,
        }
        resp_lookup = await cli.get(ITAD_LOOKUP_URL, params=lookup_params, timeout=3.0)
        if resp_lookup.status_code != 200:
            logger.debug("ITAD lookup falhou (Status: %d) para AppID %s", resp_lookup.status_code, appid_str)
            return None

        lookup_data = resp_lookup.json()
        if not lookup_data.get("found") or "game" not in lookup_data:
            return None

        game_id = lookup_data["game"].get("id")
        if not game_id:
            return None

        # Passo 2: Overview / Histórico para a região solicitada
        overview_params = {
            "key": key,
            "country": cc,
            "shops": str(STEAM_SHOP_ID),
        }
        resp_overview = await cli.post(
            ITAD_OVERVIEW_URL,
            params=overview_params,
            json=[game_id],
            timeout=3.0,
        )

        if resp_overview.status_code != 200:
            # Fallback para GET se POST não for aceito
            resp_overview = await cli.get(
                ITAD_OVERVIEW_URL,
                params={**overview_params, "id": game_id},
                timeout=3.0,
            )
            if resp_overview.status_code != 200:
                logger.debug("ITAD overview falhou (Status: %d) para Game ID %s (%s)", resp_overview.status_code, game_id, cc)
                return None

        overview_data = resp_overview.json()
        prices_list = overview_data.get("prices", [])
        if not prices_list:
            return None

        game_prices = prices_list[0]
        lowest_info = game_prices.get("lowest")
        if not lowest_info or "price" not in lowest_info:
            return None

        price_obj = lowest_info["price"]
        amount = float(price_obj.get("amount", 0.0))
        currency = price_obj.get("currency", "BRL" if cc == "BR" else "USD")
        cut = lowest_info.get("cut", 0)
        recorded_at = lowest_info.get("timestamp") or lowest_info.get("recorded") or ""
        formatted_date = format_itad_date(recorded_at)

        result = {
            "source": "ITAD",
            "amount": amount,
            "currency": currency,
            "discount_cut": cut,
            "recorded_at": recorded_at,
            "recorded_date": formatted_date,
            "shop_name": lowest_info.get("shop", {}).get("name", "Steam"),
        }

        _itad_cache.set(cache_key, result)
        return result

    except (httpx.TimeoutException, httpx.RequestError) as net_err:
        logger.warning("Timeout/Erro de rede na consulta ITAD para AppID %s: %s", appid_str, net_err)
        return None
    except Exception as exc:
        logger.warning("Exceção não tratada ao consultar ITAD: %s", exc)
        return None
