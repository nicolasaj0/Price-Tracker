"""Bateria de Testes Automatizados para a Versão v1.1 (tests/test_v1_1_features.py).
Valida:
1. Cache LRU/TTL em memória para autocomplete (Steam & eShop).
2. Campo is_dm no SQLite e função get_database_stats().
3. Destaque dinâmico para promoções 100% OFF (Free to Keep).
4. Checagem de permissões prévia no canal.
5. Funcionamento concorrente do /comparar via asyncio.gather.
6. Registro de todos os 8 Slash Commands na CommandTree.
"""

import asyncio
from datetime import datetime, timezone
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import bot
import chart
import db
import eshop
import giveaways
import steam


async def run_v1_1_tests():
    print("=" * 75)
    print("🚀 INICIANDO HOMOLOGAÇÃO DAS FUNCIONALIDADES DA VERSÃO v1.1")
    print("=" * 75)

    temp_fd, temp_db = tempfile.mkstemp(suffix="_v1_1_test.db")
    os.close(temp_fd)

    try:
        # ----------------------------------------------------------------------
        # 1. Teste de Cache LRU/TTL para Autocomplete
        # ----------------------------------------------------------------------
        print("\n[1/6] ⚡ Testando Cache LRU/TTL em Memória para Autocomplete...")
        steam._steam_autocomplete_cache._cache.clear()
        
        # 1ª chamada: vai na rede
        t0 = asyncio.get_event_loop().time()
        res1 = await steam.autocomplete_steam_games("Portal", country_code="BR")
        t_net = asyncio.get_event_loop().time() - t0

        # 2ª chamada: deve vir do cache instantaneamente
        t1 = asyncio.get_event_loop().time()
        res2 = await steam.autocomplete_steam_games("Portal", country_code="BR")
        t_cache = asyncio.get_event_loop().time() - t1

        assert len(res1) > 0, "Falha na busca de autocomplete"
        assert res1 == res2, "Cache retornou resultados divergentes"
        assert t_cache < t_net, "Cache não reduziu o tempo de resposta"
        print(f"  ✓ 1ª busca (Rede): {t_net*1000:.2f}ms | 2ª busca (Cache TTL): {t_cache*1000:.2f}ms ({len(res1)} sugestões).")

        # ----------------------------------------------------------------------
        # 2. Teste do Campo is_dm e Estatísticas do Banco
        # ----------------------------------------------------------------------
        print("\n[2/6] 🗄️ Testando Suporte a Alertas em DM e get_database_stats()...")
        await db.init_db(db_path=temp_db)

        # Inserção com is_dm = 1
        await db.add_track(
            guild_id=111,
            channel_id=222,
            user_id=333,
            platform="steam",
            game_id="620",
            game_title="Portal 2",
            target_price=10.0,
            last_price=32.99,
            currency="BRL",
            country_code="BR",
            notify_on_any_sale=1,
            is_dm=1,
            db_path=temp_db,
        )

        user_tracks = await db.get_user_tracks(333, channel_id=222, db_path=temp_db)
        assert len(user_tracks) == 1
        assert user_tracks[0]["is_dm"] == 1, "is_dm não persistido corretamente"

        await db.record_price_history("620", "steam", 32.99, currency="BRL", country_code="BR", db_path=temp_db)

        stats = await db.get_database_stats(db_path=temp_db)
        assert stats["total_tracked_games"] == 1
        assert stats["unique_games"] == 1
        assert stats["unique_users"] == 1
        assert stats["total_history_records"] == 1
        print(f"  ✓ is_dm validado com sucesso. Estatísticas: {stats}")

        # ----------------------------------------------------------------------
        # 3. Teste de Destaque para Promoções 100% OFF (Free to Keep)
        # ----------------------------------------------------------------------
        print("\n[3/6] 🎁 Testando Identidade Visual para Jogos 100% OFF...")
        giveaway_data = {
            "platform": "steam",
            "game_id": "999888",
            "title": "Jogo Promocional Gratuito",
            "is_free": False,
            "currency": "BRL",
            "country_code": "BR",
            "initial_price": 50.00,
            "current_price": 0.00,
            "discount_percent": 100,
            "initial_formatted": "R$ 50,00",
            "current_formatted": "R$ 0,00 (100% GRÁTIS)",
            "on_sale": True,
            "header_image": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/header.jpg",
            "url": "https://store.steampowered.com/app/999888/",
        }

        color_100 = bot.get_semantic_color(giveaway_data)
        assert color_100 == bot.DISCORD_GOLD, f"Cor esperada {hex(bot.DISCORD_GOLD)}, obtido {hex(color_100)}"

        card_giveaway = bot.build_info_embed(giveaway_data, color=color_100)
        assert "100% GRÁTIS" in card_giveaway.title or "FREE TO KEEP" in card_giveaway.title
        print(f"  ✓ Promoção 100% OFF formatada com cor Dourada ({hex(color_100)}) e badge no título.")

        # ----------------------------------------------------------------------
        # 4. Teste do Helper de Permissões do Canal
        # ----------------------------------------------------------------------
        print("\n[4/6] 🛡️ Testando Verificação de Permissões do Canal...")
        perms_dummy = bot.check_channel_permissions(None, None)
        assert perms_dummy["send_messages"] is True
        assert perms_dummy["embed_links"] is True
        assert perms_dummy["attach_files"] is True
        print("  ✓ Helper de permissões validado com valores padrão seguros.")

        # ----------------------------------------------------------------------
        # 5. Teste de Consulta Concorrente para /comparar (asyncio.gather) com 5 Regiões
        # ----------------------------------------------------------------------
        print("\n[5/7] 🌐 Testando Lógica Concorrente do /comparar com 5 Regiões (BR, US, CA, PT, JP)...")
        regions = ["BR", "US", "CA", "PT", "JP"]
        compare_tasks = [steam.get_steam_game_details("620", country_code=c) for c in regions]
        comp_results = await asyncio.gather(*compare_tasks)

        assert len(comp_results) == 5, f"Esperado 5 resultados regionais, obtido {len(comp_results)}"
        for r in comp_results:
            assert r is not None and "current_formatted" in r
            print(f"     • {r['country_code']}: {r['current_formatted']} ({r['currency']})")
        print("  ✓ Consulta paralela em 5 regiões finalizada com sucesso.")

        # ----------------------------------------------------------------------
        # 6. Teste de Configuração de Canal de Jogos Grátis e Giveaways
        # ----------------------------------------------------------------------
        print("\n[6/7] 🎁 Testando Módulo de Giveaways & Configuração de Canal de Avisos...")
        await db.set_free_games_channel(guild_id=12345, channel_id=98765, db_path=temp_db)
        saved_ch = await db.get_free_games_channel(guild_id=12345, db_path=temp_db)
        assert saved_ch == 98765, "Canal de jogos grátis não persistido corretamente"

        giveaway_test_id = "steam_test_gw_1"
        assert not await db.is_giveaway_posted(giveaway_test_id, guild_id=12345, db_path=temp_db)
        await db.record_posted_giveaway(giveaway_test_id, guild_id=12345, db_path=temp_db)
        assert await db.is_giveaway_posted(giveaway_test_id, guild_id=12345, db_path=temp_db)

        giveaways_list = await giveaways.get_steam_giveaways()
        assert isinstance(giveaways_list, list), "giveaways_list deve ser uma lista"
        print(f"  ✓ Giveaways ativos identificados: {len(giveaways_list)}")
        for g in giveaways_list[:2]:
            print(f"     • {g['title']} | Valor: {g['worth']} | Expira: {g['end_date']}")

        # ----------------------------------------------------------------------
        # 7. Verificação dos 10 Slash Commands na CommandTree
        # ----------------------------------------------------------------------
        print("\n[7/7] 🌲 Verificando Comandos Slash na CommandTree...")
        cmds = {cmd.name for cmd in bot.bot.tree.get_commands()}
        expected_cmds = {"steam", "eshop", "historico", "monitorar", "listar", "remover", "comparar", "status", "gratis", "canal_gratis"}
        assert expected_cmds.issubset(cmds), f"Comandos ausentes: {expected_cmds - cmds}"
        print(f"  ✓ Todos os 10 Slash Commands registrados com sucesso: {sorted(list(cmds))}")

    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)
        await steam.close_http_client()
        await eshop.close_http_client()
        await giveaways.close_http_client()

    print("\n" + "=" * 75)
    print("🎉 HOMOLOGAÇÃO DA VERSÃO v1.1 CONCLUÍDA COM 100% DE SUCESSO!")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(run_v1_1_tests())
