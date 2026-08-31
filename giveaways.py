"""Módulo de Rastreamento de Jogos Pagos 100% Grátis (Giveaways & Free to Keep) na Steam.
Consulta assíncrona com cache em memória LRU/TTL e baixo consumo de recursos (< 50 KB RAM).
"""

import asyncio
from collections import OrderedDict
from datetime import datetime
import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx

logger = logging.getLogger("PriceTracker.Giveaways")

GAMERPOWER_API_URL = "https://www.gamerpower.com/api/giveaways"
STEAM_SEARCH_FREE_URL = "https://store.steampowered.com/search/results/?query=&specials=1&maxprice=free&json=1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

_global_giveaways_client: Optional[httpx.AsyncClient] = None


class InMemoryTTLCache:
    """Cache em memória LRU com expiração TTL (Zero dependências externas, < 50 KB RAM)."""

    def __init__(self, maxsize: int = 64, ttl_seconds: float = 1800.0):  # 30 minutos de TTL
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


_giveaways_cache = InMemoryTTLCache(maxsize=32, ttl_seconds=1800.0)


async def get_http_client() -> httpx.AsyncClient:
    """Obtém ou inicializa a sessão HTTP persistente compartilhada com pooling controlado."""
    global _global_giveaways_client
    if _global_giveaways_client is None or _global_giveaways_client.is_closed:
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)
        _global_giveaways_client = httpx.AsyncClient(headers=HEADERS, timeout=10.0, limits=limits)
    return _global_giveaways_client


async def close_http_client() -> None:
    """Encerra a sessão HTTP compartilhada."""
    global _global_giveaways_client
    if _global_giveaways_client and not _global_giveaways_client.is_closed:
        await _global_giveaways_client.aclose()
        _global_giveaways_client = None


def sanitize_text(text: str) -> str:
    """Decodifica entidades HTML e limpa espaços redundantes."""
    if not text:
        return ""
    return html.unescape(text).strip()


def format_giveaway_date(date_str: str) -> str:
    """Formata data de encerramento em padrão brasileiro amigável."""
    if not date_str or date_str == "N/A":
        return "Por tempo limitado / Até durarem os estoques"
    try:
        # Formato comum: 2026-08-26 23:59:00
        dt = datetime.fromisoformat(date_str.replace(" ", "T"))
        return dt.strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return date_str


async def get_steam_giveaways(client: Optional[httpx.AsyncClient] = None) -> List[Dict[str, Any]]:
    """Busca jogos pagos que estão temporariamente 100% GRÁTIS para resgate na Steam.
    
    Retorna lista de ofertas ativas com título, valor original, data de término e link de resgate.
    """
    cached = _giveaways_cache.get("steam_giveaways")
    if cached is not None:
        return cached

    cli = client or await get_http_client()
    giveaways: List[Dict[str, Any]] = []
    seen_titles = set()

    # 1. Fonte Primária: GamerPower Giveaways API (Específica para Giveaways Steam)
    try:
        params = {
            "platform": "steam",
            "type": "game",
        }
        resp = await cli.get(GAMERPOWER_API_URL, params=params, timeout=8.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    raw_title = sanitize_text(item.get("title", ""))
                    clean_title = re.sub(r"\s*\((Steam|PC|Giveaway|Key)\)", "", raw_title, flags=re.IGNORECASE).strip()
                    clean_title = re.sub(r"\s+Giveaway$", "", clean_title, flags=re.IGNORECASE).strip()
                    
                    if not clean_title or clean_title.lower() in seen_titles:
                        continue
                    seen_titles.add(clean_title.lower())

                    worth = sanitize_text(item.get("worth", "N/A"))
                    url = item.get("open_giveaway_url") or item.get("gamerpower_url") or "https://store.steampowered.com/"
                    thumbnail = item.get("image") or item.get("thumbnail") or ""
                    end_date = format_giveaway_date(item.get("end_date", ""))
                    instructions = sanitize_text(item.get("instructions", "Resgate diretamente na Steam para vincular à sua conta."))

                    giveaways.append(
                        {
                            "id": str(item.get("id", "")),
                            "title": clean_title,
                            "worth": worth,
                            "url": url,
                            "thumbnail": thumbnail,
                            "end_date": end_date,
                            "instructions": instructions,
                            "source": "GamerPower",
                        }
                    )
    except Exception as exc:
        logger.warning("Falha ao consultar GamerPower Giveaways: %s", exc)

    # 2. Fonte Secundária: Steam Store Search (Promoções 100% OFF ativas na loja)
    try:
        resp_steam = await cli.get(STEAM_SEARCH_FREE_URL, timeout=8.0)
        if resp_steam.status_code == 200:
            steam_data = resp_steam.json()
            items = steam_data.get("items", [])
            for s_item in items:
                s_name = sanitize_text(s_item.get("name", ""))
                if not s_name or s_name.lower() in seen_titles:
                    continue
                seen_titles.add(s_name.lower())

                logo = s_item.get("logo", "")
                appid_match = re.search(r"/apps/(\d+)/", logo)
                appid = appid_match.group(1) if appid_match else ""
                store_url = f"https://store.steampowered.com/app/{appid}/" if appid else "https://store.steampowered.com/"
                header_img = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg" if appid else logo

                giveaways.append(
                    {
                        "id": appid or s_name,
                        "title": s_name,
                        "worth": "Promoção 100% OFF",
                        "url": store_url,
                        "thumbnail": header_img,
                        "end_date": "Por tempo limitado",
                        "instructions": "Adicione à sua conta diretamente na página da loja Steam.",
                        "source": "Steam Store",
                    }
                )
    except Exception as exc:
        logger.warning("Falha ao consultar Steam Store Specials: %s", exc)

    _giveaways_cache.set("steam_giveaways", giveaways)
    return giveaways
