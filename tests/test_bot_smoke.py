"""Smoke Test dos Componentes de UI, Multi-Embeds, Views e Comandos do Discord Bot (tests/test_bot_smoke.py)."""

import asyncio
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import discord
from discord import app_commands

import bot
import chart
import db
import eshop
import steam


async def run_smoke_test():
    print("=" * 70)
    print("🧪 INICIANDO SMOKE TEST DE UI, MULTI-EMBEDS E VIEWS DO BOT")
    print("=" * 70)

    # 1. Validação dos Comandos Registrados na Tree
    print("\n[1/5] 🌲 Verificando Comandos Slash na CommandTree...")
    commands = bot.bot.tree.get_commands()
    command_names = {c.name for c in commands}
    print(f"  • Comandos encontrados: {command_names}")
    expected_commands = {"steam", "eshop", "historico", "monitorar", "listar", "remover", "comparar", "status"}
    for cmd in expected_commands:
        assert cmd in command_names, f"Comando /{cmd} não encontrado na CommandTree"
        print(f"  ✓ Slash Command /{cmd} registrado com sucesso.")

    # 2. Validação da Geração de Multi-Embeds (Card 1 Info + Card 2 Chart)
    print("\n[2/5] 🎨 Testando Geração de Multi-Embed Stack...")
    steam_mock_data = {
        "platform": "steam",
        "game_id": "620",
        "title": "Portal 2",
        "is_free": False,
        "currency": "BRL",
        "initial_price": 32.99,
        "current_price": 6.59,
        "discount_percent": 80,
        "initial_formatted": "R$ 32,99",
        "current_formatted": "R$ 6,59",
        "on_sale": True,
        "header_image": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/header.jpg",
        "url": "https://store.steampowered.com/app/620/",
        "description": "The 'Perpetual Testing Initiative' has been expanded...",
        "developers": "Valve",
        "publishers": "Valve",
    }
    color_steam = bot.get_semantic_color(steam_mock_data, lowest_historical=6.59)
    assert color_steam == bot.DISCORD_GREEN, f"Cor esperada DISCORD_GREEN, obtido 0x{color_steam:X}"

    card_info = bot.build_info_embed(steam_mock_data, color=color_steam, lowest_historical=6.59)
    assert card_info.title is not None
    assert card_info.color.value == bot.DISCORD_GREEN
    print(f"  ✓ Card 1 (Info) gerado com cor verde Discord (0x{card_info.color.value:X}).")

    card_chart = bot.build_chart_embed(steam_mock_data, color=color_steam, lowest_historical=6.59, history_count=4)
    assert card_chart.image.url == "attachment://price_chart.png"
    print(f"  ✓ Card 2 (Chart) gerado com imagem 'attachment://price_chart.png'.")

    # 3. Validação das Views Interativas (Buttons, Selects e Paginator)
    print("\n[3/5] 🔘 Testando Componentes de UI (Views, Buttons e Select Menus)...")
    action_view = bot.GameActionView(steam_mock_data, is_tracked=False, user_id=12345)
    assert len(action_view.children) == 2, "GameActionView deve conter 2 botões"
    print("  ✓ GameActionView criada com botão de link e botão de monitoramento direto.")

    # Teste RemoveSelectView
    mock_tracks = [
        {"id": 1, "platform": "steam", "game_title": "Portal 2", "last_price": 6.59},
        {"id": 2, "platform": "eshop", "game_title": "Hollow Knight", "last_price": 27.99},
    ]
    remove_view = bot.RemoveSelectView(mock_tracks, user_id=12345)
    select_item = remove_view.children[0]
    assert isinstance(select_item, discord.ui.Select)
    assert len(select_item.options) == 2
    print(f"  ✓ RemoveSelectView criada com {len(select_item.options)} opções no menu suspenso.")

    # Teste PaginationView
    pagination_view = bot.PaginationView(user_id=12345, channel_id=67890, total_items=12, page_size=5)
    assert pagination_view.total_pages == 3
    print(f"  ✓ PaginationView criada com cálculo de 3 páginas para 12 itens.")

    # 4. Validação dos Autocompletes
    print("\n[4/5] ⚡ Testando Handlers de Autocomplete...")
    steam_suggs = await steam.autocomplete_steam_games("Portal")
    assert len(steam_suggs) > 0, "Autocomplete da Steam não retornou sugestões"
    print(f"  ✓ Autocomplete Steam retornou {len(steam_suggs)} sugestões para 'Portal'.")

    # 5. Validação da Geração de Gráfico
    print("\n[5/5] 📈 Testando Geração de Buffer de Gráfico...")
    mock_history = [
        ("2026-08-01T12:00:00+00:00", 32.99),
        ("2026-08-10T12:00:00+00:00", 16.49),
        ("2026-08-20T12:00:00+00:00", 6.59),
    ]
    chart_buf = await asyncio.to_thread(chart.gerar_grafico_historico, "Portal 2", mock_history)
    assert chart_buf is not None
    assert len(chart_buf.getvalue()) > 0
    print(f"  ✓ Gráfico gerado com sucesso ({len(chart_buf.getvalue()) / 1024:.2f} KB).")

    # Encerra conexões HTTP
    await steam.close_http_client()
    await eshop.close_http_client()

    print("\n" + "=" * 70)
    print("🎉 SMOKE TEST CONCLUÍDO COM 100% DE SUCESSO!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
