"""PriceTracker Discord Bot (Versão v1.1 - Produção & Telemetria)
Monitoramento de preços para Steam e Nintendo eShop em múltiplas regiões (BR, US, PT, GB, JP, CA, AR).
Multi-Embed Stack, gráficos dinâmicos, auto-restore no boot, alertas em DM, /comparar e /status.
"""

import asyncio
from datetime import datetime, timezone
import html
import io
import logging
import math
import os
import sys
import time
import tracemalloc
from typing import Any, Dict, List, Literal, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import httpx

import db
import eshop
import giveaways
import itad
import steam

# Configuração de Logging Estruturado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("PriceTracker.Bot")

# Inicia medição de memória para telemetria
if not tracemalloc.is_tracing():
    tracemalloc.start()

# Carrega variáveis de ambiente
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID")
ITAD_API_KEY = os.getenv("ITAD_API_KEY", "").strip()

# Paleta Semântica Oficial do Discord
DISCORD_GREEN = 0x57F287   # Menor preço histórico ou super desconto (>= 50%)
DISCORD_YELLOW = 0xFEE75C  # Promoção ativa padrão (< 50%)
DISCORD_BLURPLE = 0x5865F2 # Preço regular / sem desconto
DISCORD_GOLD = 0xF1C40F    # Destaque Especial: 100% OFF / Free to Keep
DISCORD_RED = 0xED4245     # Erro / Remoção

# Mapeamento de Bandeiras por Região
COUNTRY_FLAGS = {
    "BR": "🇧🇷",
    "US": "🇺🇸",
    "PT": "🇵🇹",
    "GB": "🇬🇧",
    "JP": "🇯🇵",
    "CA": "🇨🇦",
    "AR": "🇦🇷",
    "DE": "🇩🇪",
    "ES": "🇪🇸",
    "FR": "🇫🇷",
    "IT": "🇮🇹",
    "AU": "🇦🇺",
}

# Opções de Regiões nos Comandos Slash
REGIAO_CHOICES = [
    app_commands.Choice(name="🇧🇷 Brasil (BRL / R$)", value="BR"),
    app_commands.Choice(name="🇺🇸 Estados Unidos (USD / $)", value="US"),
    app_commands.Choice(name="🇵🇹 Portugal / Europa (EUR / €)", value="PT"),
    app_commands.Choice(name="🇬🇧 Reino Unido (GBP / £)", value="GB"),
    app_commands.Choice(name="🇯🇵 Japão (JPY / ¥)", value="JP"),
    app_commands.Choice(name="🇨🇦 Canadá (CAD / CA$)", value="CA"),
    app_commands.Choice(name="🇦🇷 Argentina (USD / $)", value="AR"),
]


def check_channel_permissions(channel: Any, me: Optional[discord.Member] = None) -> Dict[str, bool]:
    """Verifica as permissões do bot no canal atual para degradação graciosa."""
    if channel is None or not hasattr(channel, "permissions_for") or me is None:
        return {"send_messages": True, "embed_links": True, "attach_files": True}
    perms = channel.permissions_for(me)
    return {
        "send_messages": perms.send_messages,
        "embed_links": perms.embed_links,
        "attach_files": perms.attach_files,
    }


class PriceTrackerBot(commands.Bot):
    """Cliente Discord com gerenciamento de sessão HTTP compartilhada e telemetria."""

    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.http_session: Optional[httpx.AsyncClient] = None
        self.start_time: float = time.monotonic()

    async def setup_hook(self) -> None:
        """Inicializa banco SQLite (com auto-recuperação), pool HTTP e sincroniza comandos."""
        if BACKUP_CHANNEL_ID and BACKUP_CHANNEL_ID.strip():
            db_exists = os.path.exists(db.DB_PATH) and os.path.getsize(db.DB_PATH) > 0
            if not db_exists:
                logger.info("Arquivo de banco local ausente. Iniciando auto-recuperação a partir do Discord...")
                try:
                    channel_id_int = int(BACKUP_CHANNEL_ID.strip())
                    channel = await self.fetch_channel(channel_id_int)
                    if channel and hasattr(channel, "history"):
                        async for msg in channel.history(limit=25):
                            if msg.attachments:
                                for att in msg.attachments:
                                    if att.filename.endswith(".db"):
                                        logger.info("Snapshot encontrado: '%s' (%d bytes). Restaurando...", att.filename, att.size)
                                        db_bytes = await att.read()
                                        db._ensure_parent_dir(db.DB_PATH)
                                        with open(db.DB_PATH, "wb") as f:
                                            f.write(db_bytes)
                                        logger.info("Banco de dados restaurado com sucesso a partir do backup!")
                                        break
                            if os.path.exists(db.DB_PATH) and os.path.getsize(db.DB_PATH) > 0:
                                break
                except Exception as restore_err:
                    logger.warning("Falha durante auto-recuperação do banco via Discord: %s", restore_err)

        logger.info("Inicializando banco de dados SQLite com WAL Mode...")
        await db.init_db(db.DB_PATH)

        self.http_session = await steam.get_http_client()
        logger.info("Pool de conexões HTTP inicializado.")

    async def close(self) -> None:
        """Finalização segura fechando sessões HTTP."""
        logger.info("Encerrando sessões HTTP...")
        await steam.close_http_client()
        await eshop.close_http_client()
        await itad.close_http_client()
        await super().close()


bot = PriceTrackerBot()


# ==============================================================================
# SANITIZAÇÃO DE STRINGS & MULTI-EMBED BUILDER
# ==============================================================================

def clean_str(text: Optional[str], max_len: int = 250) -> str:
    """Sanitiza entidades HTML e trunca texto para limites do Discord."""
    if not text:
        return ""
    unescaped = html.unescape(str(text)).replace("\r\n", " ").replace("\n", " ").strip()
    return unescaped[:max_len]


def get_semantic_color(data: Dict[str, Any], lowest_historical: Optional[float] = None) -> int:
    """Retorna a cor dinâmica correspondente conforme regra de desconto e menor histórico."""
    is_sale = data.get("on_sale", False)
    current_p = data.get("current_price", 0.0)
    initial_p = data.get("initial_price", current_p)
    disc_pct = data.get("discount_percent", 0)

    # 100% OFF (Free to Keep) -> Dourado
    if disc_pct == 100 or (current_p == 0.0 and initial_p > 0.0 and not data.get("is_free")):
        return DISCORD_GOLD

    if data.get("is_free"):
        return DISCORD_GREEN

    if is_sale:
        if (lowest_historical is not None and current_p <= lowest_historical) or disc_pct >= 50:
            return DISCORD_GREEN
        return DISCORD_YELLOW

    return DISCORD_BLURPLE


def build_info_embed(
    data: Dict[str, Any],
    color: int,
    lowest_historical: Optional[float] = None,
    lowest_historical_date: Optional[str] = None,
    itad_data: Optional[Dict[str, Any]] = None,
    title_prefix: str = "",
) -> discord.Embed:
    """Gera o Embed rico de informações do jogo com suporte multi-região."""
    platform = data.get("platform", "steam")
    country_code = data.get("country_code", "BR").upper()
    flag = COUNTRY_FLAGS.get(country_code, "🌐")
    platform_name = "Steam" if platform == "steam" else "Nintendo eShop"
    platform_icon = "🎮" if platform == "steam" else "🔴"

    current_p = data.get("current_price", 0.0)
    initial_p = data.get("initial_price", current_p)
    disc_pct = data.get("discount_percent", 0)
    is_sale = data.get("on_sale", False) and current_p < initial_p

    clean_title = clean_str(data.get("title", "Jogo"), 180)

    # Tratamento especial de 100% OFF
    is_100_off = disc_pct == 100 or (current_p == 0.0 and initial_p > 0.0 and not data.get("is_free"))
    if is_100_off and not title_prefix.startswith("🎁"):
        title_prefix = "🎁 [100% GRÁTIS / FREE TO KEEP] " + title_prefix

    embed = discord.Embed(
        title=f"{title_prefix}{platform_icon} {clean_title} ({platform_name} {flag} {country_code})"[:256],
        url=data.get("url", ""),
        color=color,
    )

    dev_pub = []
    if data.get("developers") and data["developers"] != "N/A":
        dev_pub.append(clean_str(data["developers"], 100))
    if data.get("publishers") and data["publishers"] != "N/A" and data["publishers"] != data.get("developers"):
        dev_pub.append(clean_str(data["publishers"], 100))

    if dev_pub:
        embed.description = f"🏢 *{' • '.join(dev_pub)}*\n"
    elif data.get("description"):
        embed.description = clean_str(data["description"], 240) + "..."

    # Preços e Descontos
    currency = data.get("currency", "BRL")
    if is_100_off:
        embed.add_field(name="🎁 Oferta Especial", value="**100% DE DESCONTO (GRATUITO)**", inline=True)
        embed.add_field(name="🏷️ Preço Regular", value=f"~~{data['initial_formatted']}~~", inline=True)
    elif data.get("is_free"):
        embed.add_field(name="💰 Preço Atual", value="**Gratuito / Free-to-play**", inline=True)
    elif is_sale:
        embed.add_field(
            name="💵 Preço Atual",
            value=f"**{data['current_formatted']}** 🔥 `-{disc_pct}%`",
            inline=True,
        )
        embed.add_field(
            name="🏷️ Preço Regular",
            value=f"~~{data['initial_formatted']}~~",
            inline=True,
        )
    else:
        embed.add_field(
            name="💰 Preço Regular",
            value=f"**{data['current_formatted']}**",
            inline=True,
        )
        embed.add_field(name="📊 Status", value="Preço Padrão (Sem Desconto)", inline=True)

    if data.get("discount_end"):
        try:
            end_dt = datetime.fromisoformat(data["discount_end"].replace("Z", "+00:00"))
            embed.add_field(
                name="⏳ Promoção válida até",
                value=end_dt.strftime("%d/%m/%Y"),
                inline=True,
            )
        except Exception:
            pass

    # Exibição do Menor Preço Histórico Real (ITAD) ou Local
    if itad_data and itad_data.get("amount") is not None:
        itad_p = float(itad_data["amount"])
        itad_curr = itad_data.get("currency", currency)
        formatted_low = steam.format_currency_global(itad_p, currency=itad_curr, country_code=country_code)
        cut = itad_data.get("discount_cut", 0)
        date_str = f" em {itad_data.get('recorded_date')}" if itad_data.get("recorded_date") else ""
        cut_str = f" `(-{cut}%)`" if cut > 0 else ""
        embed.add_field(
            name="📉 Menor Histórico",
            value=f"**{formatted_low}**{cut_str}{date_str}",
            inline=True,
        )
    elif lowest_historical and lowest_historical > 0:
        formatted_low = steam.format_currency_global(lowest_historical, currency=currency, country_code=country_code)
        date_str = f" em {lowest_historical_date}" if lowest_historical_date else ""
        embed.add_field(
            name="📉 Menor Histórico",
            value=f"**{formatted_low}**{date_str}",
            inline=True,
        )

    banner_url = data.get("header_image")
    if banner_url and str(banner_url).startswith("http"):
        embed.set_image(url=banner_url)

    embed.set_footer(text=f"PriceTracker v1.1 • Região: {country_code} ({currency}) • Alertas a cada 4h")
    return embed





# ==============================================================================
# UI COMPONENTS (Views, Buttons, Select Menus, Pagination)
# ==============================================================================

class GameActionView(discord.ui.View):
    """View unificada com Link da Loja e Botão de Monitoramento Direto com isolamento regional."""

    def __init__(self, data: Dict[str, Any], is_tracked: bool = False, user_id: Optional[int] = None):
        super().__init__(timeout=600)
        self.data = data
        self.is_tracked = is_tracked
        self.user_id = user_id

        country_code = data.get("country_code", "BR").upper()
        store_label = f"Ver na Steam ({country_code})" if data.get("platform") == "steam" else f"Ver na eShop ({country_code})"
        if data.get("url"):
            self.add_item(
                discord.ui.Button(
                    label=store_label,
                    style=discord.ButtonStyle.link,
                    url=data["url"],
                    emoji="🔗",
                )
            )

        if not self.is_tracked:
            btn_monitor = discord.ui.Button(
                label=f"Monitorar Preço ({country_code})",
                style=discord.ButtonStyle.success,
                emoji="🔔",
                custom_id="btn_one_click_monitor",
            )
            btn_monitor.callback = self.on_one_click_monitor
            self.add_item(btn_monitor)
        else:
            btn_untrack = discord.ui.Button(
                label=f"Remover Alerta ({country_code})",
                style=discord.ButtonStyle.danger,
                emoji="❌",
                custom_id="btn_one_click_untrack",
            )
            btn_untrack.callback = self.on_one_click_untrack
            self.add_item(btn_untrack)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("Erro na GameActionView: %s", error, exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("⚠️ Não foi possível processar a ação.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Não foi possível processar a ação.", ephemeral=True)

    async def on_one_click_monitor(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        game_id = str(self.data["game_id"])
        platform = self.data.get("platform", "steam")
        game_title = clean_str(self.data.get("title", "Jogo"), 200)
        current_p = self.data.get("current_price", 0.0)
        currency = self.data.get("currency", "BRL")
        country_code = self.data.get("country_code", "BR").upper()
        flag = COUNTRY_FLAGS.get(country_code, "🌐")

        await db.add_track(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            platform=platform,
            game_id=game_id,
            game_title=game_title,
            target_price=None,
            last_price=current_p,
            currency=currency,
            country_code=country_code,
            notify_on_any_sale=1,
            is_dm=0,
            db_path=db.DB_PATH,
        )

        if current_p > 0:
            await db.record_price_history(game_id, platform, current_p, currency=currency, country_code=country_code, db_path=db.DB_PATH)

        await interaction.followup.send(
            f"✅ **{game_title}** ({flag} {country_code}) agora está sob monitoramento para você neste canal! "
            f"Você será notificado a cada nova promoção.",
            ephemeral=True,
        )

    async def on_one_click_untrack(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        country_code = self.data.get("country_code", "BR").upper()
        removed = await db.remove_tracked_game_exact(
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            platform=self.data.get("platform", ""),
            game_id=str(self.data.get("game_id", "")),
            country_code=country_code,
            db_path=db.DB_PATH,
        )

        if removed:
            await interaction.followup.send(
                f"🗑️ O monitoramento de **{clean_str(self.data.get('title', 'Jogo'), 100)}** ({country_code}) foi removido.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ Esse jogo ({country_code}) não constava na sua lista de monitoramento deste canal.",
                ephemeral=True,
            )


class RemoveSelect(discord.ui.Select):
    """Menu suspenso para remoção instantânea dos monitoramentos do usuário."""

    def __init__(self, tracks: List[Dict[str, Any]], user_id: int):
        self.user_id = user_id
        options = []
        for t in tracks[:25]:
            plat = "🎮 Steam" if t["platform"] == "steam" else "🔴 eShop"
            cc = t.get("country_code", "BR").upper()
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            currency = t.get("currency", "BRL")

            if t.get("last_price") is not None:
                last_p = steam.format_currency_global(t["last_price"], currency=currency, country_code=cc)
            else:
                last_p = "N/A"

            options.append(
                discord.SelectOption(
                    label=f"{flag} {clean_str(t['game_title'], 85)}"[:100],
                    value=str(t["id"]),
                    description=f"{plat} ({cc}) • Preço: {last_p}"[:100],
                    emoji="🗑️",
                )
            )

        super().__init__(
            placeholder="Selecione o jogo que deseja parar de monitorar...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⚠️ Apenas quem solicitou a remoção pode usar este menu.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        track_id = int(self.values[0])
        removed = await db.remove_track(track_id, self.user_id, db_path=db.DB_PATH)

        if removed:
            await interaction.followup.send(
                "🗑️ Monitoramento cancelado com sucesso!",
                ephemeral=True,
            )
            self.disabled = True
            try:
                await interaction.edit_original_response(view=self.view)
            except Exception:
                pass
        else:
            await interaction.followup.send(
                "❌ Erro ao remover alerta. Ele pode já ter sido excluído.",
                ephemeral=True,
            )


class RemoveSelectView(discord.ui.View):
    def __init__(self, tracks: List[Dict[str, Any]], user_id: int):
        super().__init__(timeout=180)
        self.add_item(RemoveSelect(tracks, user_id))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class PaginationView(discord.ui.View):
    """Navegação paginada para a listagem de jogos com timeout gracioso."""

    def __init__(self, user_id: int, channel_id: int, total_items: int, page_size: int = 5):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.channel_id = channel_id
        self.page_size = page_size
        self.current_page = 1
        self.total_items = total_items
        self.total_pages = max(1, math.ceil(total_items / page_size))
        self._update_buttons()

    def _update_buttons(self):
        self.btn_prev.disabled = self.current_page <= 1
        self.btn_next.disabled = self.current_page >= self.total_pages
        self.btn_counter.label = f"Página {self.current_page}/{self.total_pages}"

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    async def get_page_embed(self) -> discord.Embed:
        items, _ = await db.get_user_tracked_games_paginated(
            user_id=self.user_id,
            channel_id=self.channel_id,
            page=self.current_page,
            page_size=self.page_size,
            db_path=db.DB_PATH,
        )

        embed = discord.Embed(
            title="📋 Seus Jogos Monitorados",
            color=DISCORD_BLURPLE,
            description=(
                f"Alertas ativos para <@{self.user_id}> neste canal:\n"
                f"**Total cadastrado:** `{self.total_items}` jogos\n"
            ),
        )

        if not items:
            embed.description += "\n*Nenhum jogo nesta página.*"
            return embed

        for g in items:
            plat = "🎮 Steam" if g["platform"] == "steam" else "🔴 eShop"
            cc = g.get("country_code", "BR").upper()
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            currency = g.get("currency", "BRL")
            dm_badge = " • 📬 *DM*" if g.get("is_dm") else ""

            if g.get("last_price") is not None:
                last_p = steam.format_currency_global(g["last_price"], currency=currency, country_code=cc)
            else:
                last_p = "N/A"

            if g.get("target_price"):
                target_p = steam.format_currency_global(g["target_price"], currency=currency, country_code=cc)
            else:
                target_p = "Qualquer desconto"

            g_title = clean_str(g["game_title"], 75)

            embed.add_field(
                name=f"#{g['id']} • {flag} {g_title} ({plat} {cc}){dm_badge}",
                value=(
                    f"💵 **Último Preço:** `{last_p}` | 🎯 **Alvo:** `{target_p}`\n"
                    f"🆔 `ID:` `{g['game_id']}` | 🌍 `Região:` `{cc}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━"
                ),
                inline=False,
            )

        embed.set_footer(text="Para remover um alerta, use o comando /remover.")
        return embed

    @discord.ui.button(label="◀️ Anterior", style=discord.ButtonStyle.primary, custom_id="nav_prev")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ Apenas quem solicitou pode navegar.", ephemeral=True)
            return
        self.current_page = max(1, self.current_page - 1)
        self._update_buttons()
        embed = await self.get_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Pág 1/1", style=discord.ButtonStyle.secondary, disabled=True, custom_id="nav_counter")
    async def btn_counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="Próximo ▶️", style=discord.ButtonStyle.primary, custom_id="nav_next")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⚠️ Apenas quem solicitou pode navegar.", ephemeral=True)
            return
        self.current_page = min(self.total_pages, self.current_page + 1)
        self._update_buttons()
        embed = await self.get_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# ==============================================================================
# HELPER DE ENVIO MULTI-EMBED
# ==============================================================================

async def send_game_response(
    interaction: discord.Interaction,
    details: Dict[str, Any],
    title_prefix: str = "",
) -> None:
    """Monta a resposta com Embed rico e banner anexado diretamente (ultra-rápido, zero impacto de RAM)."""
    platform = details.get("platform", "steam")
    game_id = str(details.get("game_id", ""))
    current_p = details.get("current_price", 0.0)
    currency = details.get("currency", "BRL")
    country_code = details.get("country_code", "BR").upper()

    if current_p > 0:
        await db.record_price_history(game_id, platform, current_p, currency=currency, country_code=country_code, db_path=db.DB_PATH)

    lowest_rec_tuple = await db.get_lowest_historical_record(platform, game_id, country_code=country_code, db_path=db.DB_PATH)
    lowest_rec = lowest_rec_tuple[0] if lowest_rec_tuple else None
    lowest_rec_date = lowest_rec_tuple[1] if lowest_rec_tuple else None

    # Consulta Menor Preço Histórico Real no IsThereAnyDeal se for Steam
    itad_data = None
    if platform == "steam":
        itad_data = await itad.get_steam_all_time_low(game_id, country_code=country_code)

    effective_lowest = lowest_rec
    if itad_data and itad_data.get("amount") is not None:
        itad_amt = float(itad_data["amount"])
        effective_lowest = itad_amt if lowest_rec is None else min(lowest_rec, itad_amt)

    color = get_semantic_color(details, lowest_historical=effective_lowest)

    is_tracked = await db.is_game_tracked_by_user(
        interaction.channel_id, interaction.user.id, platform, game_id, country_code=country_code, db_path=db.DB_PATH
    )
    view = GameActionView(details, is_tracked=is_tracked, user_id=interaction.user.id)

    # Checagem de permissões do bot no canal
    me = interaction.guild.me if interaction.guild else None
    perms = check_channel_permissions(interaction.channel, me)

    # Fallback para texto caso não haja permissão de Embed Links
    if not perms["embed_links"]:
        text_response = (
            f"**{details.get('title', 'Jogo')}** ({platform.upper()} {country_code})\n"
            f"💵 **Preço Atual:** {details.get('current_formatted')}\n"
            f"🔗 Link da Loja: {details.get('url', 'N/A')}"
        )
        await interaction.followup.send(text_response, view=view)
        return

    card_info = build_info_embed(
        details,
        color=color,
        lowest_historical=lowest_rec,
        lowest_historical_date=lowest_rec_date,
        itad_data=itad_data,
        title_prefix=title_prefix,
    )

    files_to_send = []
    banner_url = details.get("header_image")
    if banner_url and str(banner_url).startswith("http") and perms.get("attach_files", True):
        try:
            cli = bot.http_session or await steam.get_http_client()
            resp_banner = await cli.get(banner_url, timeout=3.0)
            if resp_banner.status_code == 200 and len(resp_banner.content) > 500:
                files_to_send.append(discord.File(fp=io.BytesIO(resp_banner.content), filename="banner.jpg"))
                card_info.set_image(url="attachment://banner.jpg")
        except Exception as exc:
            logger.warning("Falha ao anexar banner localmente, usando URL remota: %s", exc)

    try:
        if files_to_send:
            await interaction.followup.send(embed=card_info, files=files_to_send, view=view)
        else:
            await interaction.followup.send(embed=card_info, view=view)
    except Exception as send_err:
        logger.error("Erro ao enviar embed com anexo: %s. Tentando envio simples...", send_err)
        if banner_url:
            card_info.set_image(url=banner_url)
        await interaction.followup.send(embed=card_info, view=view)


# ==============================================================================
# SLASH COMMANDS (v1.1: /steam, /eshop, /historico, /monitorar, /comparar, /status)
# ==============================================================================

@bot.tree.command(name="steam", description="Busca um jogo na Steam em qualquer região (BR, US, PT, GB, JP, CA, AR).")
@app_commands.describe(
    jogo="Nome ou AppID do jogo na Steam (autocomplete ativo)",
    regiao="País/Região para consulta de preço (Padrão: Brasil)",
)
@app_commands.choices(regiao=REGIAO_CHOICES)
async def cmd_steam(interaction: discord.Interaction, jogo: str, regiao: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer()
    cc = regiao.value if regiao else "BR"
    logger.info("/steam solicitado por %s: %s (Região: %s)", interaction.user, jogo, cc)

    try:
        if jogo.strip().isdigit():
            details = await steam.get_steam_game_details(jogo.strip(), country_code=cc)
        else:
            results = await steam.search_steam_games(jogo, limit=1, country_code=cc)
            details = await steam.get_steam_game_details(results[0]["id"], country_code=cc) if results else None

        if not details:
            await interaction.followup.send(
                f"❌ Nenhum jogo encontrado na Steam ({cc}) para o termo: **{clean_str(jogo, 100)}**.",
                ephemeral=True,
            )
            return

        await send_game_response(interaction, details)

    except Exception as exc:
        logger.error("Erro no comando /steam: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao consultar a Steam. Tente novamente mais tarde.", ephemeral=True)


@cmd_steam.autocomplete("jogo")
async def steam_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    if len(current.strip()) < 2:
        return []
    regiao_val = interaction.namespace.regiao or "BR"
    try:
        suggestions = await steam.autocomplete_steam_games(current, country_code=regiao_val)
        return [app_commands.Choice(name=name, value=val) for name, val in suggestions[:25]]
    except Exception:
        return []


@bot.tree.command(name="eshop", description="Busca um jogo na Nintendo eShop em qualquer região oficial (BR, US, PT, GB, JP, CA).")
@app_commands.describe(
    jogo="Nome ou NSUID do jogo no catálogo Nintendo (autocomplete ativo)",
    regiao="País/Região para consulta de preço (Padrão: Brasil)",
)
@app_commands.choices(regiao=REGIAO_CHOICES)
async def cmd_eshop(interaction: discord.Interaction, jogo: str, regiao: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer()
    cc = regiao.value if regiao else "BR"
    logger.info("/eshop solicitado por %s: %s (Região: %s)", interaction.user, jogo, cc)

    try:
        if jogo.strip().isdigit() and len(jogo.strip()) >= 10:
            details = await eshop.get_eshop_game_details(jogo.strip(), country_code=cc)
        else:
            results = await eshop.search_eshop_games(jogo, limit=1, country_code=cc)
            details = await eshop.get_eshop_game_details(results[0]["id"], country_code=cc, title_fallback=results[0]["name"]) if results else None

        if not details:
            await interaction.followup.send(
                f"❌ Nenhum jogo encontrado na Nintendo eShop ({cc}) para o termo: **{clean_str(jogo, 100)}**.",
                ephemeral=True,
            )
            return

        await send_game_response(interaction, details)

    except Exception as exc:
        logger.error("Erro no comando /eshop: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao consultar a Nintendo eShop.", ephemeral=True)


@cmd_eshop.autocomplete("jogo")
async def eshop_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    if len(current.strip()) < 2:
        return []
    regiao_val = interaction.namespace.regiao or "BR"
    try:
        suggestions = await eshop.autocomplete_eshop_games(current, country_code=regiao_val)
        return [app_commands.Choice(name=name, value=val) for name, val in suggestions[:25]]
    except Exception:
        return []


@bot.tree.command(name="comparar", description="Compara o preço do jogo em 4 regiões simultaneamente (BR, US, PT/EU, JP).")
@app_commands.describe(
    plataforma="Plataforma do jogo (Steam ou eShop)",
    jogo="Nome ou identificador do jogo (com autocomplete)",
)
async def cmd_comparar(
    interaction: discord.Interaction,
    plataforma: Literal["Steam", "eShop"],
    jogo: str,
):
    """Compara preços em tempo real disparando consultas concorrentes via asyncio.gather."""
    await interaction.response.defer()
    plat_key = plataforma.lower()
    logger.info("/comparar [%s] '%s' por %s", plat_key, jogo, interaction.user)

    try:
        regions_to_compare = ["BR", "US", "CA", "PT", "JP"]

        # Identifica ID do jogo
        if plat_key == "steam":
            if jogo.strip().isdigit():
                game_id = jogo.strip()
            else:
                res = await steam.search_steam_games(jogo, limit=1, country_code="BR")
                if not res:
                    await interaction.followup.send(f"❌ Jogo **{clean_str(jogo, 100)}** não encontrado na Steam.", ephemeral=True)
                    return
                game_id = res[0]["id"]

            tasks_list = [steam.get_steam_game_details(game_id, country_code=c) for c in regions_to_compare]
        else:
            if jogo.strip().isdigit() and len(jogo.strip()) >= 10:
                game_id = jogo.strip()
            else:
                res = await eshop.search_eshop_games(jogo, limit=1, country_code="BR")
                if not res:
                    await interaction.followup.send(f"❌ Jogo **{clean_str(jogo, 100)}** não encontrado na eShop.", ephemeral=True)
                    return
                game_id = res[0]["id"]

            tasks_list = [eshop.get_eshop_game_details(game_id, country_code=c) for c in regions_to_compare]

        results = await asyncio.gather(*tasks_list, return_exceptions=True)

        valid_results = [r for r in results if isinstance(r, dict) and r is not None]
        if not valid_results:
            await interaction.followup.send("⚠️ Não foi possível obter dados regionais para comparação.", ephemeral=True)
            return

        base_game = valid_results[0]
        game_title = clean_str(base_game.get("title", "Jogo"), 180)
        banner = base_game.get("header_image")

        embed = discord.Embed(
            title=f"🌐 Comparativo Global de Preços: {game_title}",
            description=f"Preços oficiais coletados em tempo real na **{plataforma}**:\n",
            color=DISCORD_BLURPLE,
        )

        for res in valid_results:
            cc = res.get("country_code", "BR").upper()
            flag = COUNTRY_FLAGS.get(cc, "🌐")
            price_fmt = res.get("current_formatted", "N/A")
            disc = res.get("discount_percent", 0)
            disc_badge = f" 🔥 `-{disc}%`" if disc > 0 else ""
            currency = res.get("currency", "BRL")

            embed.add_field(
                name=f"{flag} {cc} ({currency})",
                value=f"**{price_fmt}**{disc_badge}",
                inline=True,
            )

        if banner and str(banner).startswith("http"):
            embed.set_thumbnail(url=banner)

        embed.set_footer(text="PriceTracker v1.1 • Valores cotados nas moedas oficiais de cada loja")
        await interaction.followup.send(embed=embed)

    except Exception as exc:
        logger.error("Erro no comando /comparar: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao comparar preços regionais.", ephemeral=True)


@cmd_comparar.autocomplete("jogo")
async def comparar_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    if len(current.strip()) < 2:
        return []
    plat = str(interaction.namespace.plataforma or "Steam").lower()
    try:
        if plat == "steam":
            items = await steam.autocomplete_steam_games(current, country_code="BR")
        else:
            items = await eshop.autocomplete_eshop_games(current, country_code="BR")
        return [app_commands.Choice(name=name, value=val) for name, val in items[:25]]
    except Exception:
        return []





@bot.tree.command(name="monitorar", description="Cadastra um alerta de preço no SQLite com opção de notificação em DM.")
@app_commands.describe(
    plataforma="Plataforma do jogo (Steam ou eShop)",
    jogo="Nome ou identificador do jogo (com autocomplete)",
    preco_alvo="Preço máximo desejado na moeda da região (Opcional - deixe vazio para alertar em qualquer promoção)",
    regiao="País/Região para monitoramento (Padrão: Brasil)",
    privado="Enviar alerta via Mensagem Direta (DM) em vez de notificar no canal público",
)
@app_commands.choices(regiao=REGIAO_CHOICES)
async def cmd_monitorar(
    interaction: discord.Interaction,
    plataforma: Literal["Steam", "eShop"],
    jogo: str,
    preco_alvo: Optional[float] = None,
    regiao: Optional[app_commands.Choice[str]] = None,
    privado: bool = False,
):
    await interaction.response.defer()
    plat_key = plataforma.lower()
    cc = regiao.value if regiao else "BR"
    flag = COUNTRY_FLAGS.get(cc, "🌐")
    logger.info("/monitorar [%s] '%s' (alvo: %s, Região: %s, DM: %s) por %s", plat_key, jogo, preco_alvo, cc, privado, interaction.user)

    try:
        details = None
        if plat_key == "steam":
            if jogo.strip().isdigit():
                details = await steam.get_steam_game_details(jogo.strip(), country_code=cc)
            else:
                results = await steam.search_steam_games(jogo, limit=1, country_code=cc)
                if results:
                    details = await steam.get_steam_game_details(results[0]["id"], country_code=cc)
        else:
            if jogo.strip().isdigit() and len(jogo.strip()) >= 10:
                details = await eshop.get_eshop_game_details(jogo.strip(), country_code=cc)
            else:
                results = await eshop.search_eshop_games(jogo, limit=1, country_code=cc)
                if results:
                    details = await eshop.get_eshop_game_details(results[0]["id"], country_code=cc, title_fallback=results[0]["name"])

        if not details:
            await interaction.followup.send(
                f"❌ Não foi possível localizar o jogo **{clean_str(jogo, 100)}** na {plataforma} ({cc}).",
                ephemeral=True,
            )
            return

        current_p = details["current_price"]
        currency = details.get("currency", "BRL")
        game_id = str(details["game_id"])
        game_title = clean_str(details["title"], 200)
        target = float(preco_alvo) if (preco_alvo is not None and preco_alvo > 0) else None

        await db.add_track(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            platform=plat_key,
            game_id=game_id,
            game_title=game_title,
            target_price=target,
            last_price=current_p,
            currency=currency,
            country_code=cc,
            notify_on_any_sale=1 if target is None else 0,
            is_dm=1 if privado else 0,
            db_path=db.DB_PATH,
        )

        if current_p > 0:
            await db.record_price_history(game_id, plat_key, current_p, currency=currency, country_code=cc, db_path=db.DB_PATH)

        if target:
            target_text = steam.format_currency_global(target, currency=currency, country_code=cc)
        else:
            target_text = "Qualquer Promoção / Queda de Preço"

        dm_info = "📬 **Notificação:** Mensagem Direta (DM)" if privado else "📢 **Notificação:** Canal Público"

        embed = discord.Embed(
            title=f"✅ Alerta de Preço Ativado! ({flag} {cc})",
            color=DISCORD_GREEN,
            description=f"O jogo **{game_title}** ({flag} {cc}) agora está sob monitoramento para você.\n{dm_info}",
        )
        embed.add_field(name="🎮 Plataforma", value=f"{plataforma} ({cc})", inline=True)
        embed.add_field(name="💵 Preço Atual", value=details["current_formatted"], inline=True)
        embed.add_field(name="🎯 Alvo de Alerta", value=f"**{target_text}**", inline=True)
        if details.get("header_image"):
            embed.set_thumbnail(url=details["header_image"])
        embed.set_footer(text=f"Moeda: {currency} • Notificação automática assim que o critério for atingido.")

        view = GameActionView(details, is_tracked=True, user_id=interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)

    except Exception as exc:
        logger.error("Erro no comando /monitorar: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao cadastrar monitoramento.", ephemeral=True)


@cmd_monitorar.autocomplete("jogo")
async def monitorar_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    if len(current.strip()) < 2:
        return []
    plat = str(interaction.namespace.plataforma or "Steam").lower()
    regiao_val = interaction.namespace.regiao or "BR"
    try:
        if plat == "steam":
            items = await steam.autocomplete_steam_games(current, country_code=regiao_val)
        else:
            items = await eshop.autocomplete_eshop_games(current, country_code=regiao_val)
        return [app_commands.Choice(name=name, value=val) for name, val in items[:25]]
    except Exception:
        return []


@bot.tree.command(name="listar", description="Exibe a lista de jogos rastreados por você neste canal (com paginação e região).")
async def cmd_listar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        user_games = await db.get_user_tracks(interaction.user.id, interaction.channel_id, db_path=db.DB_PATH)
        if not user_games:
            await interaction.followup.send(
                "ℹ️ Você não possui jogos sendo monitorados neste canal. Use `/monitorar` ou o botão de ação para começar!",
                ephemeral=True,
            )
            return

        paginator = PaginationView(
            user_id=interaction.user.id,
            channel_id=interaction.channel_id,
            total_items=len(user_games),
            page_size=5,
        )
        embed = await paginator.get_page_embed()
        await interaction.followup.send(embed=embed, view=paginator, ephemeral=True)

    except Exception as exc:
        logger.error("Erro no comando /listar: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao consultar sua lista de monitoramento.", ephemeral=True)


@bot.tree.command(name="remover", description="Apresenta um menu suspenso para remoção instantânea dos seus alertas.")
async def cmd_remover(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        user_games = await db.get_user_tracks(interaction.user.id, interaction.channel_id, db_path=db.DB_PATH)
        if not user_games:
            await interaction.followup.send(
                "ℹ️ Você não possui jogos monitorados neste canal para remover.",
                ephemeral=True,
            )
            return

        view = RemoveSelectView(user_games, interaction.user.id)
        embed = discord.Embed(
            title="🗑️ Gerenciamento de Alertas",
            color=DISCORD_YELLOW,
            description="Selecione abaixo o jogo que deseja parar de monitorar neste canal:",
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    except Exception as exc:
        logger.error("Erro no comando /remover: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao carregar opções de remoção.", ephemeral=True)


@bot.tree.command(name="gratis", description="Lista jogos pagos da Steam que estão temporariamente 100% GRÁTIS para resgatar.")
async def cmd_gratis(interaction: discord.Interaction):
    """Comando para descobrir jogos pagos da Steam em promoção 100% OFF (Free to Keep)."""
    await interaction.response.defer()
    logger.info("/gratis solicitado por %s", interaction.user)

    try:
        items = await giveaways.get_steam_giveaways()
        if not items:
            embed = discord.Embed(
                title="🎁 Jogos Grátis na Steam (Free to Keep)",
                color=0x95a5a6,
                description=(
                    "ℹ️ No momento **não há nenhum jogo pago com promoção de 100% OFF** ativa para resgate permanente na Steam.\n\n"
                    "*(Jogos sempre gratuitos/Free-to-Play não são listados aqui, apenas promoções temporárias de jogos pagos).*"
                ),
            )
            embed.set_footer(text="PriceTracker v1.1 • Atualizado a cada 30min")
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(
            title=f"🎁 Jogos 100% Grátis na Steam ({len(items)} disponíveis)",
            color=0xf1c40f,  # Dourado Lendário 100% OFF
            description="Aproveite para resgatar e adicionar permanentemente à sua biblioteca Steam:",
        )

        view = discord.ui.View(timeout=180)

        for idx, item in enumerate(items[:5], start=1):
            title = item.get("title", "Jogo Steam")
            worth = item.get("worth", "N/A")
            end_date = item.get("end_date", "Por tempo limitado")
            url = item.get("url", "https://store.steampowered.com/")
            instructions_txt = item.get("instructions", "Resgate diretamente na loja da Steam.")
            clean_inst = instructions_txt.replace("\n", " ").strip()
            if len(clean_inst) > 130:
                clean_inst = clean_inst[:127] + "..."

            field_val = (
                f"💰 **Valor Original:** `{worth}` ➔ **GRÁTIS!**\n"
                f"⏳ **Expira em:** {end_date}\n"
                f"📝 {clean_inst}"
            )
            embed.add_field(name=f"🎮 {idx}. {title}", value=field_val, inline=False)

            button_label = f"Resgatar {title}"[:80]
            view.add_item(discord.ui.Button(label=button_label, url=url, style=discord.ButtonStyle.link, emoji="🎁"))

        first_thumb = items[0].get("thumbnail")
        if first_thumb and str(first_thumb).startswith("http"):
            embed.set_image(url=first_thumb)

        embed.set_footer(text="PriceTracker v1.1 • Giveaways & Free to Keep • Cache 30min")
        await interaction.followup.send(embed=embed, view=view)

    except Exception as exc:
        logger.error("Erro no comando /gratis: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao consultar promoções gratuitas na Steam.", ephemeral=True)


@bot.tree.command(name="canal_gratis", description="Define ou remove o canal de avisos de jogos 100% grátis na Steam (Free to Keep).")
@app_commands.describe(
    canal="Canal de texto onde os alertas serão enviados (deixe vazio para desativar)",
)
@app_commands.default_permissions(manage_guild=True)
async def cmd_canal_gratis(
    interaction: discord.Interaction,
    canal: Optional[discord.TextChannel] = None,
):
    """Configura o canal de alertas automáticos de jogos grátis da Steam no servidor."""
    if not interaction.guild_id:
        await interaction.response.send_message("❌ Este comando deve ser executado dentro de um servidor.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        if canal is not None:
            me = interaction.guild.me if interaction.guild else None
            perms = check_channel_permissions(canal, me)
            if not perms["send_messages"] or not perms["embed_links"]:
                await interaction.followup.send(
                    f"⚠️ O bot não possui permissão de **Enviar Mensagens** e **Inserir Links** no canal {canal.mention}.",
                    ephemeral=True,
                )
                return

            await db.set_free_games_channel(interaction.guild_id, canal.id, db_path=db.DB_PATH)
            embed = discord.Embed(
                title="🎁 Canal de Jogos Grátis Configurado!",
                description=(
                    f"O canal {canal.mention} foi definido com sucesso para receber alertas de **jogos 100% grátis da Steam (Free to Keep)**.\n\n"
                    f"• **Frequência:** Verificação automática a cada 1 hora\n"
                    f"• **Menções:** Nenhuma (sem spam de `@everyone`)\n"
                    f"• **Conteúdo:** Embed com valor original, banner e botão de resgate"
                ),
                color=0xF1C40F,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await db.set_free_games_channel(interaction.guild_id, None, db_path=db.DB_PATH)
            await interaction.followup.send("🔕 Alertas automáticos de jogos grátis foram **desativados** neste servidor.", ephemeral=True)

    except Exception as exc:
        logger.error("Erro no comando /canal_gratis: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao configurar canal de jogos grátis.", ephemeral=True)


@bot.tree.command(name="status", description="Exibe a telemetria do bot, tempo de atividade, uso de RAM e dados do SQLite.")
async def cmd_status(interaction: discord.Interaction):
    """Comando de telemetria e diagnóstico do bot."""
    await interaction.response.defer(ephemeral=True)
    try:
        # 1. Latência do WebSocket
        latency_ms = round(bot.latency * 1000, 1)

        # 2. Uptime
        uptime_seconds = int(time.monotonic() - bot.start_time)
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{days}d {hours}h {minutes}m {seconds}s" if days > 0 else f"{hours}h {minutes}m {seconds}s"

        # 3. Estatísticas do SQLite
        stats = await db.get_database_stats(db.DB_PATH)

        # 4. Uso de Memória RAM (tracemalloc)
        _, peak_mem = tracemalloc.get_traced_memory()
        peak_mb = peak_mem / (1024 * 1024)

        embed = discord.Embed(
            title="⚡ Painel de Telemetria & Diagnóstico",
            color=DISCORD_GREEN if latency_ms < 200 else DISCORD_YELLOW,
            description=f"Status operacional da instância PriceTracker v1.1.",
        )

        embed.add_field(name="🏓 Latência Gateway", value=f"`{latency_ms} ms`", inline=True)
        embed.add_field(name="⏱️ Uptime", value=f"`{uptime_str}`", inline=True)
        embed.add_field(name="🧠 Consumo RAM (Pico)", value=f"`{peak_mb:.2f} MB`", inline=True)

        embed.add_field(name="🎮 Jogos Monitorados", value=f"`{stats['total_tracked_games']}` inscrições", inline=True)
        embed.add_field(name="👥 Usuários Ativos", value=f"`{stats['unique_users']}` usuários", inline=True)
        embed.add_field(name="📈 Histórico Coletado", value=f"`{stats['total_history_records']}` registros", inline=True)

        itad_configured = bool(os.getenv("ITAD_API_KEY", "").strip())
        itad_status = "🟢 Ativo (All-Time Low)" if itad_configured else "⚪ Desativado (Fallback Local)"
        embed.add_field(name="🌐 IsThereAnyDeal (ITAD)", value=f"`{itad_status}`", inline=True)

        embed.add_field(
            name="🌍 Regiões Oficiais Ativas",
            value="🇧🇷 BR • 🇺🇸 US • 🇵🇹 PT • 🇬🇧 GB • 🇯🇵 JP • 🇨🇦 CA • 🇦🇷 AR",
            inline=False,
        )

        embed.set_footer(text=f"Database: {db.DB_PATH} • Online Backup API Ativa")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as exc:
        logger.error("Erro no comando /status: %s", exc, exc_info=True)
        await interaction.followup.send("⚠️ Erro ao obter telemetria do sistema.", ephemeral=True)


# ==============================================================================
# BACKGROUND WORKERS (PREÇOS 4H COM SUPORTE A DM & BACKUP A QUENTE 24H)
# ==============================================================================

@tasks.loop(hours=4)
async def price_checker_worker():
    """Loop periódico de 4 horas checando preços por região com suporte a DM e auto-purge."""
    logger.info("Iniciando ciclo periódico de checagem de preços multi-região...")
    try:
        unique_games = await db.get_unique_tracked_games(db_path=db.DB_PATH)
        if not unique_games:
            logger.info("Nenhum jogo ativo no monitoramento.")
            return

        logger.info("Checando preços para %d combinações únicas de jogo/região...", len(unique_games))

        for item in unique_games:
            platform = item["platform"]
            game_id = item["game_id"]
            country_code = item.get("country_code", "BR").upper()

            try:
                details = None
                if platform == "steam":
                    details = await steam.get_steam_game_details(game_id, country_code=country_code)
                elif platform == "eshop":
                    details = await eshop.get_eshop_game_details(game_id, country_code=country_code, title_fallback=item.get("game_title", ""))

                if not details:
                    logger.warning("Não foi possível obter dados de [%s] ID %s (%s)", platform, game_id, country_code)
                    continue

                current_p = details["current_price"]
                on_sale = details["on_sale"]
                currency = details.get("currency", "BRL")
                lowest_rec = await db.get_lowest_historical_price(platform, game_id, country_code=country_code, db_path=db.DB_PATH)

                subscribers = await db.get_subscribers_for_game(platform, game_id, country_code=country_code, db_path=db.DB_PATH)
                for sub in subscribers:
                    target_p = sub["target_price"]
                    last_p = sub["last_price"]
                    channel_id = sub["channel_id"]
                    user_id = sub["user_id"]
                    notify_any = sub.get("notify_on_any_sale", 1)
                    is_dm = sub.get("is_dm", 0)

                    should_notify = False
                    reason = ""

                    if target_p is not None and target_p > 0:
                        if current_p <= target_p and (last_p is None or current_p < last_p or last_p > target_p):
                            should_notify = True
                            formatted_target = steam.format_currency_global(target_p, currency=currency, country_code=country_code)
                            reason = f"🎯 O preço atingiu sua meta de **{formatted_target}**!"
                    elif (notify_any or target_p is None) and on_sale and (last_p is None or current_p < last_p):
                        should_notify = True
                        if lowest_rec and current_p <= lowest_rec:
                            reason = "🌟 **O jogo atingiu o menor preço já registrado nesta região!**"
                        else:
                            reason = "🔥 **O jogo entrou em promoção!**"

                    if should_notify:
                        try:
                            color = get_semantic_color(details, lowest_historical=lowest_rec)
                            card_info = build_info_embed(
                                details, color=color, lowest_historical=lowest_rec, title_prefix="🚨 ALERTA DE PREÇO: "
                            )
                            card_info.insert_field_at(
                                0,
                                name="📢 Motivo do Alerta",
                                value=reason,
                                inline=False,
                            )
                            view = GameActionView(details, is_tracked=True, user_id=user_id)

                            # Caso o usuário tenha optado por notificação via DM
                            if is_dm:
                                try:
                                    user_obj = bot.get_user(user_id) or await bot.fetch_user(user_id)
                                    if user_obj:
                                        await user_obj.send(
                                            content="🔔 Notificação de Preço Privada!",
                                            embed=card_info,
                                            view=view,
                                        )
                                        logger.info("Alerta privado enviado via DM para usuário %s", user_id)
                                        continue
                                except discord.Forbidden:
                                    logger.warning("DM fechada para o usuário %s. Realizando fallback para o canal %s.", user_id, channel_id)

                            # Envio no canal público
                            channel = bot.get_channel(channel_id)
                            if channel is None:
                                channel = await bot.fetch_channel(channel_id)

                            if channel and hasattr(channel, "send"):
                                await channel.send(
                                    content=f"🔔 <@{user_id}> Notificação de Preço!",
                                    embed=card_info,
                                    view=view,
                                )
                                logger.info("Notificação enviada para usuário %s no canal %s (%s)", user_id, channel_id, country_code)

                        except (discord.NotFound, discord.Forbidden) as perm_err:
                            logger.warning("Canal %s inacessível (%s). Auto-purge...", channel_id, perm_err)
                            purged = await db.remove_tracks_by_channel(channel_id, db_path=db.DB_PATH)
                            logger.info("Purge concluído: %d registros removidos para o canal %s.", purged, channel_id)

                        except Exception as send_err:
                            logger.error("Erro ao enviar mensagem no canal %s: %s", channel_id, send_err)

                await db.update_price(platform, game_id, current_p, country_code=country_code, db_path=db.DB_PATH)
                if current_p > 0:
                    await db.record_price_history(game_id, platform, current_p, currency=currency, country_code=country_code, db_path=db.DB_PATH)

                await asyncio.sleep(1.0)

            except Exception as item_err:
                logger.error("Erro ao processar [%s] %s (%s): %s", platform, game_id, country_code, item_err)
                await asyncio.sleep(1.5)

        logger.info("Ciclo periódico concluído com sucesso.")

    except Exception as exc:
        logger.error("Exceção geral no worker periódico: %s", exc, exc_info=True)


@tasks.loop(hours=1)
async def check_free_games_feed():
    """Verifica novos jogos 100% grátis na Steam (Free to Keep) e anuncia nos canais configurados sem @everyone."""
    try:
        channels = await db.get_all_free_games_channels(db_path=db.DB_PATH)
        if not channels:
            return

        active_giveaways = await giveaways.get_steam_giveaways(client=bot.http_session)
        if not active_giveaways:
            return

        for guild_id, channel_id in channels:
            try:
                ch = bot.get_channel(channel_id)
                if ch is None:
                    try:
                        ch = await bot.fetch_channel(channel_id)
                    except Exception:
                        continue
                if not ch:
                    continue

                for item in active_giveaways:
                    g_id = str(item.get("id"))
                    already_posted = await db.is_giveaway_posted(g_id, guild_id, db_path=db.DB_PATH)
                    if already_posted:
                        continue

                    embed = discord.Embed(
                        title=f"🎁 Novo Jogo 100% Grátis na Steam: {clean_str(item.get('title', 'Jogo'), 180)}",
                        description=(
                            f"**{clean_str(item.get('title', 'Jogo'), 180)}** está gratuito por tempo limitado!\n"
                            f"Resgate para sua conta Steam e ele será **seu para sempre** na biblioteca.\n"
                        ),
                        color=0xF1C40F,
                        url=item.get("url", "https://store.steampowered.com"),
                    )

                    embed.add_field(
                        name="💰 Preço Original",
                        value=f"~~{item.get('worth', '$0.00')}~~ ➔ **GRÁTIS (100% OFF)**",
                        inline=True,
                    )
                    embed.add_field(
                        name="⏳ Disponibilidade",
                        value=f"`{item.get('end_date', 'Por tempo limitado')}`",
                        inline=True,
                    )

                    instructions_txt = item.get("instructions", "Resgate diretamente na página do jogo na Steam.")
                    clean_inst = instructions_txt.replace("\n", " ").strip()
                    if len(clean_inst) > 130:
                        clean_inst = clean_inst[:127] + "..."
                    embed.add_field(
                        name="📝 Como Resgatar",
                        value=f"*{clean_inst}*",
                        inline=False,
                    )

                    thumb = item.get("thumbnail")
                    if thumb and str(thumb).startswith("http"):
                        embed.set_image(url=thumb)

                    embed.set_footer(text="PriceTracker • Alerta Automático de Jogos Grátis Steam (Sem @everyone)")

                    view = discord.ui.View()
                    view.add_item(
                        discord.ui.Button(
                            label="Resgatar na Steam",
                            style=discord.ButtonStyle.link,
                            url=item.get("url", "https://store.steampowered.com"),
                            emoji="🎁",
                        )
                    )

                    # Envio direto sem menção @everyone
                    await ch.send(embed=embed, view=view)
                    await db.record_posted_giveaway(g_id, guild_id, db_path=db.DB_PATH)
                    logger.info("Giveaway '%s' anunciado no canal %s (Guild %s)", item.get("title"), channel_id, guild_id)
                    await asyncio.sleep(1.0)

            except Exception as guild_err:
                logger.error("Erro ao processar feed grátis na guilda %s: %s", guild_id, guild_err)

    except Exception as exc:
        logger.error("Erro no worker de jogos grátis: %s", exc, exc_info=True)


@tasks.loop(hours=24)
async def database_backup_worker():
    """Worker de Backup a Quente do SQLite (24h) com envio consolidado para canal privado do Discord."""
    if not BACKUP_CHANNEL_ID or not BACKUP_CHANNEL_ID.strip():
        return

    try:
        channel_id_int = int(BACKUP_CHANNEL_ID.strip())
        channel = bot.get_channel(channel_id_int)
        if channel is None:
            channel = await bot.fetch_channel(channel_id_int)

        if not channel:
            logger.warning("Canal de backup %s não acessível.", BACKUP_CHANNEL_ID)
            return

        logger.info("Iniciando rotina de backup a quente do SQLite via Online Backup API...")
        backup_buf = await db.criar_backup_local(db.DB_PATH)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        file = discord.File(fp=backup_buf, filename="backup_bot_database.db")

        embed = discord.Embed(
            title="💾 Backup a Quente do Banco de Dados",
            description=(
                f"Snapshot íntegro do SQLite consolidado via Online Backup API.\n"
                f"⏰ **Timestamp:** `{now_str}`\n"
                f"📦 **Tamanho:** `{len(backup_buf.getvalue()) / 1024:.2f} KB`"
            ),
            color=DISCORD_BLURPLE,
        )
        embed.set_footer(text="PriceTracker v1.1 • Sistema de Auto-Recuperação para Disco Efêmero")

        if hasattr(channel, "history"):
            try:
                async for old_msg in channel.history(limit=15):
                    if old_msg.author.id == bot.user.id and old_msg.attachments:
                        for att in old_msg.attachments:
                            if att.filename.endswith(".db"):
                                try:
                                    await old_msg.delete()
                                except Exception:
                                    pass
            except Exception:
                pass

        await channel.send(embed=embed, file=file)
        logger.info("Backup a quente enviado com sucesso para o canal %s.", BACKUP_CHANNEL_ID)

    except Exception as exc:
        logger.error("Erro no worker de backup do SQLite: %s", exc, exc_info=True)


@price_checker_worker.before_loop
async def before_price_checker_worker():
    await bot.wait_until_ready()


@price_checker_worker.error
async def price_checker_worker_error(error: Exception):
    logger.critical("Exceção não tratada no price_checker_worker: %s", error, exc_info=True)


@database_backup_worker.before_loop
async def before_database_backup_worker():
    await bot.wait_until_ready()


@database_backup_worker.error
async def database_backup_worker_error(error: Exception):
    logger.critical("Exceção não tratada no database_backup_worker: %s", error, exc_info=True)



# ==============================================================================
# EVENTOS DO BOT
# ==============================================================================

@bot.event
async def on_ready():
    logger.info("=" * 60)
    logger.info("PriceTracker Bot Conectado: %s (ID: %s)", bot.user.name, bot.user.id)

    try:
        synced = await bot.tree.sync()
        logger.info("Slash Commands sincronizados com sucesso: %d comandos ativos.", len(synced))
    except Exception as exc:
        logger.error("Erro ao sincronizar comandos Slash: %s", exc)

    if not price_checker_worker.is_running():
        price_checker_worker.start()
        logger.info("Worker de checagem a cada 4 horas iniciado.")

    if not check_free_games_feed.is_running():
        check_free_games_feed.start()
        logger.info("Worker de alertas de jogos grátis Steam (1h) iniciado.")

    if BACKUP_CHANNEL_ID and BACKUP_CHANNEL_ID.strip():
        if not database_backup_worker.is_running():
            database_backup_worker.start()
            logger.info("Worker de backup diário a quente iniciado (Canal: %s).", BACKUP_CHANNEL_ID)

    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="preços globais | /comparar | /status",
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)
    logger.info("PriceTracker v1.1 pronto para operação!")
    logger.info("=" * 60)


def main():
    if not DISCORD_TOKEN:
        logger.critical(
            "DISCORD_TOKEN não encontrado! Crie o arquivo .env a partir de .env.example e configure seu token."
        )
        sys.exit(1)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
