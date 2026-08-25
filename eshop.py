"""Módulo de Integração com as APIs da Nintendo eShop com Suporte Global Multi-Região.
Roteamento inteligente entre Algolia (Américas) e Solr (Europa/Global) com cache LRU/TTL em memória.
"""

import asyncio
from collections import OrderedDict
import html
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx

logger = logging.getLogger("PriceTracker.eShop")

ESHOP_PRICE_URL = "https://api.ec.nintendo.com/v1/price"
ESHOP_EUROPE_SEARCH_URL = "https://searching.nintendo-europe.com/{lang}/select"
ALGOLIA_URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_HEADERS = {
    "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9",
    "x-algolia-application-id": "U3B6GR4UA3",
    "Content-Type": "application/json",
}

DEFAULT_ESHOP_BANNER = "https://assets.nintendo.com/image/upload/v1/ncom/en_US/games/switch/nintendo-switch-logo"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.nintendo.com",
    "Referer": "https://www.nintendo.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
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


_eshop_autocomplete_cache = InMemoryTTLCache(maxsize=256, ttl_seconds=600.0)


async def get_http_client() -> httpx.AsyncClient:
    """Obtém ou inicializa a sessão HTTP persistente compartilhada."""
    global _global_client
    if _global_client is None or _global_client.is_closed:
        _global_client = httpx.AsyncClient(headers=HEADERS, timeout=12.0)
    return _global_client


async def close_http_client() -> None:
    """Encerra a sessão HTTP compartilhada."""
    global _global_client
    if _global_client and not _global_client.is_closed:
        await _global_client.aclose()
        _global_client = None


def get_eshop_lang(country_code: str) -> str:
    """Retorna código de idioma oficial para a região."""
    cc = country_code.upper().strip()
    if cc == "BR":
        return "pt"
    elif cc in ("PT",):
        return "pt"
    elif cc in ("ES", "AR", "MX", "CL", "CO"):
        return "es"
    elif cc in ("DE", "AT"):
        return "de"
    elif cc in ("FR",):
        return "fr"
    elif cc in ("IT",):
        return "it"
    elif cc in ("JP",):
        return "ja"
    return "en"


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


def _clean_fetch_url(url: str) -> str:
    """Remove wrappers de proxy/fetch (ex: image/fetch/q_auto/f_auto/https://...) que causam falhas no Discord."""
    if not url:
        return ""
    if "image/fetch" in url:
        idx = url.find("image/fetch")
        nested = url[idx + 11:]
        m = re.search(r"https?://[^\s\"']+", nested)
        if m:
            return m.group(0)
    return url


def _extract_clean_image_url(hit: Dict[str, Any], default: str = DEFAULT_ESHOP_BANNER) -> str:
    """Extrai a URL de imagem widescreen de maior qualidade e 100% compatível com Discord Embeds."""
    # 1. Prioridade: Imagem principal de produto widescreen do Cloudinary (1200px)
    pimg = hit.get("productImage")
    if pimg:
        if isinstance(pimg, str) and not pimg.startswith("http"):
            clean_path = pimg.lstrip("/")
            if not clean_path.endswith((".jpg", ".png", ".jpeg", ".webp")):
                clean_path += ".jpg"
            return f"https://assets.nintendo.com/image/upload/c_fill,w_1200/{clean_path}"
        cleaned = _clean_fetch_url(str(pimg))
        if cleaned:
            if not cleaned.endswith((".jpg", ".png", ".jpeg", ".webp")) and "assets.nintendo.com" in cleaned:
                cleaned += ".jpg"
            return cleaned

    # 2. Prioridade: Imagem quadrada / Box Art (limpa)
    sq = hit.get("productImageSquare")
    if sq:
        cleaned = _clean_fetch_url(str(sq))
        if cleaned:
            if not cleaned.endswith((".jpg", ".png", ".jpeg", ".webp")) and "assets.nintendo.com" in cleaned:
                cleaned += ".jpg"
            return cleaned

    # 3. Prioridade: Galeria de imagens
    gallery = hit.get("productGallery") or []
    for g in gallery:
        if isinstance(g, dict) and g.get("resourceType") == "image" and g.get("publicId"):
            pid = str(g["publicId"]).lstrip("/")
            if not pid.endswith((".jpg", ".png", ".jpeg", ".webp")):
                pid += ".jpg"
            return f"https://assets.nintendo.com/image/upload/c_fill,w_1200/{pid}"

    return default


def _parse_algolia_price_details(eshop_det: Optional[Dict[str, Any]], country_code: str = "BR") -> Optional[Dict[str, Any]]:
    """Extrai informações de preço de forma instantânea e confiável a partir do eshopDetails do Algolia."""
    if not eshop_det or not isinstance(eshop_det, dict):
        return None
    reg_p = eshop_det.get("regularPrice")
    disc_p = eshop_det.get("discountPrice")
    curr = eshop_det.get("currency") or ("BRL" if country_code.upper() == "BR" else "USD")

    if reg_p is not None:
        init_val = float(reg_p)
        if disc_p is not None:
            current_val = float(disc_p)
            disc_perc = round(((init_val - current_val) / init_val) * 100) if init_val > 0 else 0
            on_sale = True
        else:
            current_val = init_val
            disc_perc = 0
            on_sale = False

        return {
            "sales_status": "onsale" if eshop_det.get("isPurchasable", True) else "unreleased",
            "country": country_code.upper(),
            "currency": curr,
            "current_price": current_val,
            "initial_price": init_val,
            "discount_percent": disc_perc,
            "current_formatted": format_currency_global(current_val, curr, country_code),
            "initial_formatted": format_currency_global(init_val, curr, country_code),
            "on_sale": on_sale,
            "discount_end": eshop_det.get("discountPriceEnd"),
        }
    elif eshop_det.get("isPurchasable") is False or reg_p is None:
        return {
            "sales_status": "not_purchasable",
            "country": country_code.upper(),
            "currency": curr,
            "current_price": 0.0,
            "initial_price": 0.0,
            "discount_percent": 0,
            "current_formatted": "Indisponível / Bônus",
            "initial_formatted": "Indisponível",
            "on_sale": False,
            "discount_end": None,
        }
    return None


async def _fetch_with_retry(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
    max_retries: int = 3,
) -> Optional[Dict[str, Any]]:
    """Executa requisição HTTP GET com connection pooling, backoff exponencial e jitter."""
    cli = client or await get_http_client()
    for attempt in range(max_retries):
        try:
            resp = await cli.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait_time = (2 ** attempt) * 1.5 + random.uniform(0.3, 1.2)
                logger.warning(
                    "Rate limit na eShop (429). Aguardando %.2fs antes da tentativa %d...",
                    wait_time,
                    attempt + 1,
                )
                await asyncio.sleep(wait_time)
            elif resp.status_code >= 500:
                logger.warning("eShop indisponível (%d). Tentativa %d/%d", resp.status_code, attempt + 1, max_retries)
                await asyncio.sleep(1.0 + attempt * 0.5)
            else:
                logger.error("Erro eShop HTTP %d para URL: %s", resp.status_code, url)
                break
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning("Falha de rede com eShop: %s. Tentativa %d/%d", exc, attempt + 1, max_retries)
            await asyncio.sleep(1.2 * (attempt + 1))
        except Exception as exc:
            logger.error("Erro inesperado na chamada eShop: %s", exc)
            break
    return None


async def get_eshop_price_by_nsuid(
    nsuid: str,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """Obtém o preço oficial da Nintendo eShop para a região solicitada."""
    cc = country_code.upper().strip()
    lang = get_eshop_lang(cc)

    countries_to_try = [(cc, lang)]
    if cc != "US":
        countries_to_try.append(("US", "en"))
    if cc != "PT" and cc != "GB":
        countries_to_try.append(("PT", "pt"))

    for c_try, l_try in countries_to_try:
        params = {
            "country": c_try,
            "lang": l_try,
            "ids": str(nsuid),
        }
        data = await _fetch_with_retry(ESHOP_PRICE_URL, params, client=client)
        if not data or "prices" not in data or not data["prices"]:
            continue

        price_data = data["prices"][0]
        sales_status = price_data.get("sales_status", "")
        regular = price_data.get("regular_price")
        discount = price_data.get("discount_price")

        if regular or discount:
            currency = (
                discount.get("currency")
                if discount
                else regular.get("currency", "BRL" if c_try == "BR" else "USD")
            )

            if discount:
                current_price = float(discount.get("raw_value", 0.0))
                initial_price = float(regular.get("raw_value", current_price)) if regular else current_price
                discount_percent = (
                    round(((initial_price - current_price) / initial_price) * 100)
                    if initial_price > 0
                    else 0
                )
                return {
                    "sales_status": sales_status,
                    "country": c_try,
                    "currency": currency,
                    "current_price": current_price,
                    "initial_price": initial_price,
                    "discount_percent": discount_percent,
                    "current_formatted": discount.get("amount") or format_currency_global(current_price, currency, c_try),
                    "initial_formatted": regular.get("amount") or format_currency_global(initial_price, currency, c_try),
                    "on_sale": True,
                    "discount_end": discount.get("end_datetime"),
                }
            elif regular:
                regular_val = float(regular.get("raw_value", 0.0))
                return {
                    "sales_status": sales_status,
                    "country": c_try,
                    "currency": currency,
                    "current_price": regular_val,
                    "initial_price": regular_val,
                    "discount_percent": 0,
                    "current_formatted": regular.get("amount") or format_currency_global(regular_val, currency, c_try),
                    "initial_formatted": regular.get("amount") or format_currency_global(regular_val, currency, c_try),
                    "on_sale": False,
                }

    return {
        "sales_status": "not_found",
        "country": cc,
        "currency": "BRL" if cc == "BR" else "USD",
        "current_price": 0.0,
        "initial_price": 0.0,
        "discount_percent": 0,
        "current_formatted": "Indisponível / Gratuito",
        "initial_formatted": "Indisponível",
        "on_sale": False,
    }


async def _search_algolia_americas(
    query: str,
    limit: int = 5,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """Busca jogos na API das Américas da Nintendo (Brasil, EUA, Canadá, etc.)."""
    cli = client or await get_http_client()
    cc = country_code.upper().strip()

    if cc == "BR":
        index_names = ["store_game_pt_br", "store_game_en_us"]
    elif cc == "CA":
        index_names = ["store_game_en_ca", "store_game_en_us"]
    else:
        index_names = ["store_game_en_us", "store_game_pt_br"]

    body = {
        "requests": [
            {
                "indexName": idx,
                "params": f"query={query}&hitsPerPage={limit}&filters=visibleInSearch:true",
            }
            for idx in index_names
        ]
    }
    try:
        resp = await cli.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=body, timeout=8.0)
        if resp.status_code != 200:
            return []

        data = resp.json()
        results = []
        seen_ids = set()

        for res in data.get("results", []):
            for hit in res.get("hits", []):
                nsuid = hit.get("nsuid")
                if not nsuid or nsuid in seen_ids:
                    continue
                seen_ids.add(nsuid)

                raw_title = hit.get("title", "Jogo Nintendo")
                title = sanitize_text(raw_title)[:200]
                banner = _extract_clean_image_url(hit)
                url_path = hit.get("url", "")
                store_url = (
                    f"https://www.nintendo.com{url_path}"
                    if url_path.startswith("/")
                    else f"https://www.nintendo.com/store/products/{nsuid}/"
                )

                price_info = _parse_algolia_price_details(hit.get("eshopDetails"), country_code=cc)
                price_str = price_info["current_formatted"] if price_info else "Preço sob consulta"
                disc_perc = price_info["discount_percent"] if price_info else 0

                results.append(
                    {
                        "id": str(nsuid),
                        "name": title,
                        "price_formatted": price_str,
                        "discount_percent": disc_perc,
                        "header_image": banner,
                        "url": store_url,
                        "description": sanitize_text(hit.get("description", ""))[:500],
                        "publishers": sanitize_text(hit.get("softwarePublisher", "")) or "Nintendo",
                        "developers": sanitize_text(hit.get("softwareDeveloper", "")) or "Nintendo",
                        "country_code": cc,
                    }
                )

                if len(results) >= limit:
                    return results

        return results
    except Exception as exc:
        logger.warning("Falha na busca Algolia Americas: %s", exc)
        return []


async def search_eshop_games(
    query: str,
    limit: int = 5,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """Busca jogos na Nintendo eShop roteando conforme a região (Américas vs Europa/Global)."""
    query_clean = query.strip()
    if not query_clean:
        return []

    cc = country_code.upper().strip()
    lang = get_eshop_lang(cc)

    if query_clean.isdigit() and len(query_clean) >= 10:
        detail = await get_eshop_game_details(query_clean, country_code=cc, client=client)
        if detail:
            return [
                {
                    "id": detail["game_id"],
                    "name": detail["title"],
                    "price_formatted": detail["current_formatted"],
                    "discount_percent": detail["discount_percent"],
                    "header_image": detail["header_image"],
                    "url": detail["url"],
                    "country_code": cc,
                }
            ]

    if cc in ("BR", "US", "CA", "MX", "AR", "CL", "CO"):
        americas_results = await _search_algolia_americas(query_clean, limit=limit, country_code=cc, client=client)
        if americas_results:
            return americas_results

    search_url = ESHOP_EUROPE_SEARCH_URL.format(lang=lang if lang in ("pt", "en", "es", "de", "fr", "it") else "en")
    params = {
        "q": query_clean,
        "fq": "type:GAME",
        "wt": "json",
        "rows": limit * 2,
    }
    data = await _fetch_with_retry(search_url, params, client=client)
    if not data or "response" not in data or "docs" not in data["response"]:
        return await _search_algolia_americas(query_clean, limit=limit, country_code=cc, client=client)

    docs = data["response"]["docs"]
    results = []

    for doc in docs:
        nsuid_list = doc.get("nsuid_txt")
        if not nsuid_list:
            continue

        nsuid = nsuid_list[0] if isinstance(nsuid_list, list) else str(nsuid_list)
        title = sanitize_text(doc.get("title", "Jogo Nintendo"))[:200]
        image_url = _clean_fetch_url(doc.get("image_url_h2x") or doc.get("image_url") or DEFAULT_ESHOP_BANNER)
        relative_url = doc.get("url", "")
        store_url = (
            f"https://www.nintendo.com{relative_url}"
            if relative_url.startswith("/")
            else f"https://www.nintendo.com/store/products/{nsuid}/"
        )

        price_info = await get_eshop_price_by_nsuid(nsuid, country_code=cc, client=client)
        if price_info:
            price_str = price_info["current_formatted"]
            discount_perc = price_info["discount_percent"]
        else:
            price_str = "Preço sob consulta"
            discount_perc = 0

        results.append(
            {
                "id": str(nsuid),
                "name": title,
                "price_formatted": price_str,
                "discount_percent": discount_perc,
                "header_image": image_url,
                "url": store_url,
                "excerpt": sanitize_text(doc.get("excerpt", ""))[:500],
                "publishers": sanitize_text(doc.get("publisher", "")) or "Nintendo",
                "country_code": cc,
            }
        )

        if len(results) >= limit:
            break

    return results


async def autocomplete_eshop_games(
    current: str,
    country_code: str = "BR",
    client: Optional[httpx.AsyncClient] = None,
) -> List[Tuple[str, str]]:
    """Gera sugestões de autocomplete em tempo real com cache em memória LRU/TTL."""
    current_clean = current.strip().lower()
    if not current_clean or len(current_clean) < 2:
        return []

    cc = country_code.upper().strip()
    cache_key = f"eshop_{cc}_{current_clean}"
    cached = _eshop_autocomplete_cache.get(cache_key)
    if cached is not None:
        return cached

    results = await search_eshop_games(current_clean, limit=15, country_code=country_code, client=client)
    choices = []
    for r in results:
        label = f"{r['name']} ({r['price_formatted']})"[:95]
        choices.append((label, r["id"]))

    _eshop_autocomplete_cache.set(cache_key, choices)
    return choices


async def get_eshop_game_details(
    nsuid: str,
    country_code: str = "BR",
    title_fallback: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """Obtém detalhes completos, imagem e preço oficial na região solicitada."""
    cc = country_code.upper().strip()
    title = sanitize_text(title_fallback) or f"Nintendo Switch Game ({nsuid})"
    banner = DEFAULT_ESHOP_BANNER
    store_url = f"https://www.nintendo.com/store/products/{nsuid}/"
    description = ""
    publishers = "Nintendo"
    developers = "Nintendo"

    price_info = None
    try:
        cli = client or await get_http_client()
        body = {
            "requests": [
                {
                    "indexName": "store_game_pt_br" if cc == "BR" else "store_game_en_us",
                    "params": f"query={nsuid}&hitsPerPage=1",
                }
            ]
        }
        resp = await cli.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=body, timeout=6.0)
        if resp.status_code == 200:
            hits = resp.json().get("results", [{}])[0].get("hits", [])
            if hits:
                hit = hits[0]
                title = sanitize_text(hit.get("title", title))[:240]
                banner = _extract_clean_image_url(hit, default=banner)
                description = sanitize_text(hit.get("description", ""))[:500]
                publishers = sanitize_text(hit.get("softwarePublisher", "")) or publishers
                developers = sanitize_text(hit.get("softwareDeveloper", "")) or developers
                if hit.get("url"):
                    store_url = f"https://www.nintendo.com{hit['url']}"
                price_info = _parse_algolia_price_details(hit.get("eshopDetails"), country_code=cc)
    except Exception:
        pass

    if not price_info:
        price_info = await get_eshop_price_by_nsuid(nsuid, country_code=cc, client=client)

    if not price_info:
        return None

    currency = price_info.get("currency", "BRL" if cc == "BR" else "USD")
    initial_p = price_info.get("initial_price", 0.0)
    current_p = price_info.get("current_price", 0.0)
    discount_perc = price_info.get("discount_percent", 0)
    on_sale = price_info.get("on_sale", False)

    return {
        "platform": "eshop",
        "game_id": str(nsuid),
        "title": title,
        "is_free": current_p == 0.0 and price_info.get("sales_status") != "not_found",
        "currency": currency,
        "country_code": cc,
        "initial_price": initial_p,
        "current_price": current_p,
        "discount_percent": discount_perc,
        "initial_formatted": price_info.get("initial_formatted") or format_currency_global(initial_p, currency, cc),
        "current_formatted": price_info.get("current_formatted") or format_currency_global(current_p, currency, cc),
        "on_sale": on_sale,
        "discount_end": price_info.get("discount_end"),
        "header_image": banner,
        "url": store_url,
        "description": description,
        "publishers": publishers,
        "developers": developers,
    }
