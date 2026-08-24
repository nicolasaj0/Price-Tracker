"""Módulo de Integração Otimizado com a API da Steam com Suporte Global Multi-Região.
Cache LRU/TTL em memória para autocomplete, backoff exponencial e tratamento de jogos 100% OFF.
"""

import asyncio
from collections import OrderedDict
import html
import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx

logger = logging.getLogger("PriceTracker.Steam")

STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
DEFAULT_STEAM_BANNER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/header.jpg"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
}

_global_client: Optional[httpx.AsyncClient] = None


class InMemoryTTLCache:
    """Cache em memória LRU com expiração TTL (Zero dependências externas, < 500 KB RAM)."""

    def __init__(self, maxsize: int = 256, ttl_seconds: float = 600.0):
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


_steam_autocomplete_cache = InMemoryTTLCache(maxsize=256, ttl_seconds=600.0)


async def get_http_client() -> httpx.AsyncClient:
    """Obtém ou inicializa a sessão HTTP persistente compartilhada."""
    global _global_client
    if _global_client is None or _global_client.is_closed:
        _global_client = httpx.AsyncClient(headers=HEADERS, timeout=12.0)
    return _global_client


async def close_http_client() -> None:
    """Encerra a sessão HTTP compartilhada no shutdown do bot."""
    global _global_client
    if _global_client and not _global_client.is_closed:
        await _global_client.aclose()
        _global_client = None


def get_steam_lang(country_code: str) -> str:
    """Mapeia código de país para o idioma correspondente na Steam."""
    cc = country_code.upper()
    if cc == "BR":
        return "brazilian"
    elif cc in ("PT",):
        return "portuguese"
    elif cc in ("ES", "AR", "MX", "CL", "CO", "PE"):
        return "spanish"
    elif cc in ("JP",):
        return "japanese"
    elif cc in ("DE", "AT"):
        return "german"
    elif cc in ("FR",):
        return "french"
    elif cc in ("IT",):
        return "italian"
    return "english"


def format_currency_global(val: float, currency: str = "BRL", country_code: str = "BR") -> str:
    """Formata valor numérico no padrão monetário nativo de sua moeda/país."""
    curr = currency.upper().strip()
    cc = country_code.upper().strip()

    if curr == "BRL" or cc == "BR":
        return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    elif curr == "USD":
        if cc == "AR":
            return f"${val:,.2f} USD"
        return f"${val:,.2f}"
    elif curr == "EUR":
        return f"{val:,.2f} €".replace(".", ",")
    elif curr == "GBP":
        return f"£{val:,.2f}"
    elif curr == "JPY":
        return f"¥{val:,.0f}"
    elif curr == "CAD":
        return f"CA${val:,.2f}"
    elif curr == "AUD":
        return f"AU${val:,.2f}"
    return f"{curr} {val:,.2f}"


format_currency_brl = format_currency_global


def sanitize_text(text: str) -> str:
    """Decodifica entidades HTML e limpa espaços redundantes."""
    if not text:
        return ""
    return html.unescape(text).strip()


async def _fetch_with_retry(
    url: str,
    params: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
    max_retries: int = 3,
) -> Optional[Dict[str, Any]]:
    """Executa requisição HTTP com connection pooling, backoff exponencial e jitter."""
    cli = client or await get_http_client()
    for attempt in range(max_retries):
        try:
            resp = await cli.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait_time = (2 ** attempt) * 1.5 + random.uniform(0.3, 1.2)
                logger.warning(
                    "Rate limit na Steam (429). Aguardando %.2fs antes da tentativa %d...",
                    wait_time,
                    attempt + 1,
                )
                await asyncio.sleep(wait_time)
            elif resp.status_code >= 500:
                logger.warning("Steam indisponível (%d). Tentativa %d/%d", resp.status_code, attempt + 1, max_retries)
                await asyncio.sleep(1.0 + attempt * 0.5)
            else:
                logger.error("Erro Steam HTTP %d para URL: %s", resp.status_code, url)
                break
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning("Timeout/Rede com Steam: %s. Tentativa %d/%d", exc, attempt + 1, max_retries)
            await asyncio.sleep(1.2 * (attempt + 1))
        except Exception as exc:
            logger.error("Exceção inesperada na API da Steam: %s", exc)
            break
    return None


async def search_steam_games(
    query: str,
    limit: int = 5,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """Busca jogos na Steam na região/moeda solicitada."""
    query_clean = query.strip()
    if not query_clean:
        return []

    cc = country_code.lower().strip()
    lang = get_steam_lang(country_code)

    if query_clean.isdigit():
        detail = await get_steam_game_details(query_clean, country_code=country_code, client=client)
        if detail:
            return [
                {
                    "id": detail["game_id"],
                    "name": detail["title"],
                    "price_formatted": detail["current_formatted"],
                    "discount_percent": detail["discount_percent"],
                    "header_image": detail["header_image"],
                    "url": detail["url"],
                    "country_code": country_code.upper(),
                }
            ]

    params = {
        "term": query_clean,
        "l": lang,
        "cc": cc,
    }
    data = await _fetch_with_retry(STEAM_SEARCH_URL, params, client=client)
    if not data or "items" not in data:
        return []

    results = []
    for item in data.get("items", [])[:limit]:
        appid = str(item.get("id"))
        raw_name = item.get("name", "Jogo Steam")
        name = sanitize_text(raw_name)[:200]
        price_info = item.get("price")

        if price_info:
            final_val = price_info.get("final", 0) / 100.0
            price_str = format_currency_global(final_val, country_code=country_code)
            discount = price_info.get("discount_percent", 0)
        else:
            price_str = "Gratuito / Indisponível"
            discount = 0

        header_img = f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"

        results.append(
            {
                "id": appid,
                "name": name,
                "price_formatted": price_str,
                "discount_percent": discount,
                "header_image": header_img,
                "url": f"https://store.steampowered.com/app/{appid}/",
                "country_code": country_code.upper(),
            }
        )
    return results


async def autocomplete_steam_games(
    current: str,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> List[Tuple[str, str]]:
    """Gera sugestões de autocomplete em tempo real com cache em memória LRU/TTL."""
    current_clean = current.strip().lower()
    if not current_clean or len(current_clean) < 2:
        return []

    cc = country_code.lower().strip()
    cache_key = f"steam_{cc}_{current_clean}"
    cached = _steam_autocomplete_cache.get(cache_key)
    if cached is not None:
        return cached

    lang = get_steam_lang(country_code)
    params = {
        "term": current_clean,
        "l": lang,
        "cc": cc,
    }
    data = await _fetch_with_retry(STEAM_SEARCH_URL, params, client=client)
    if not data or "items" not in data:
        return []

    choices = []
    for item in data.get("items", [])[:20]:
        appid = str(item.get("id"))
        name = sanitize_text(item.get("name", "Jogo"))
        price_info = item.get("price")

        if price_info:
            final_cents = price_info.get("final", 0)
            disc = price_info.get("discount_percent", 0)
            p_str = format_currency_global(final_cents / 100.0, country_code=country_code)
            label = f"{name} ({p_str}{f' -{disc}%' if disc > 0 else ''})"
        else:
            label = f"{name} (Gratuito/Indisp.)"

        label = label[:95]
        choices.append((label, appid))

    _steam_autocomplete_cache.set(cache_key, choices)
    return choices


async def get_steam_game_details(
    appid: str,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """Obtém detalhes completos, moeda nativa e preço oficial da Steam para a região."""
    cc = country_code.lower().strip()
    lang = get_steam_lang(country_code)

    params = {
        "appids": str(appid),
        "cc": cc,
        "l": lang,
    }
    data = await _fetch_with_retry(STEAM_APPDETAILS_URL, params, client=client)
    if not data or str(appid) not in data:
        return None

    app_data = data[str(appid)]
    if not app_data.get("success"):
        return None

    d = app_data.get("data", {})
    raw_title = d.get("name", f"Steam App {appid}")
    title = sanitize_text(raw_title)[:240]
    is_free = d.get("is_free", False)

    header_image = d.get("header_image") or f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
    store_url = f"https://store.steampowered.com/app/{appid}/"
    price_overview = d.get("price_overview")

    genres_list = [g.get("description", "") for g in d.get("genres", []) if g.get("description")]
    genres_str = " • ".join(genres_list[:3]) if genres_list else ""
    devs_str = sanitize_text(", ".join(d.get("developers", []))) or "N/A"
    pubs_str = sanitize_text(", ".join(d.get("publishers", []))) or "N/A"

    currency = "BRL" if country_code.upper() == "BR" else "USD"

    if is_free:
        initial_price = 0.0
        current_price = 0.0
        discount_percent = 0
        initial_formatted = "Gratuito"
        current_formatted = "Gratuito"
        on_sale = False
    elif price_overview:
        currency = price_overview.get("currency", currency)
        initial_cents = price_overview.get("initial", 0)
        final_cents = price_overview.get("final", 0)

        divisor = 1.0 if currency.upper() in ("JPY", "KRW") else 100.0
        initial_price = round(initial_cents / divisor, 2)
        current_price = round(final_cents / divisor, 2)
        discount_percent = price_overview.get("discount_percent", 0)

        # Detecta promoções 100% OFF (Free to Keep)
        if current_price == 0.0 and initial_price > 0.0:
            discount_percent = 100
            current_formatted = "R$ 0,00 (100% GRÁTIS)" if currency.upper() == "BRL" else "$0.00 (100% FREE)"
        else:
            current_formatted = price_overview.get("final_formatted") or format_currency_global(
                current_price, currency=currency, country_code=country_code
            )

        initial_formatted = price_overview.get("initial_formatted") or format_currency_global(
            initial_price, currency=currency, country_code=country_code
        )
        on_sale = discount_percent > 0 or current_price < initial_price
    else:
        initial_price = 0.0
        current_price = 0.0
        discount_percent = 0
        initial_formatted = "Indisponível"
        current_formatted = "Indisponível"
        on_sale = False

    raw_desc = d.get("short_description") or ""
    clean_desc = sanitize_text(raw_desc)[:500]

    return {
        "platform": "steam",
        "game_id": str(appid),
        "title": title,
        "is_free": is_free,
        "currency": currency,
        "country_code": country_code.upper(),
        "initial_price": initial_price,
        "current_price": current_price,
        "discount_percent": discount_percent,
        "initial_formatted": initial_formatted,
        "current_formatted": current_formatted,
        "on_sale": on_sale,
        "header_image": header_image,
        "url": store_url,
        "description": clean_desc,
        "developers": devs_str,
        "publishers": pubs_str,
        "genres": genres_str,
    }
