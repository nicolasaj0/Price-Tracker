"""Script de Teste e Validação da Infraestrutura de Backend e APIs (Steam & eShop) (tests/test_fetchers.py)."""

import asyncio
import os
import sys
import tempfile
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import db
import eshop
import steam


async def run_tests():
    print("=" * 70)
    print("🧪 INICIANDO VALIDAÇÃO DO BACKEND & FETCHERS DE PREÇO")
    print("=" * 70)

    # --------------------------------------------------------------------------
    # 1. Validação do Banco de Dados SQLite (db.py)
    # --------------------------------------------------------------------------
    print("\n[1/3] 🗄️ Testando Camada de Persistência SQLite (db.py)...")
    temp_fd, temp_db_path = tempfile.mkstemp(suffix="_test.db")
    os.close(temp_fd)

    try:
        await db.init_db(db_path=temp_db_path)
        print("  ✓ Banco inicializado com tabelas e índices.")

        # Teste de inserção
        track_id = await db.add_track(
            guild_id=1001,
            channel_id=2001,
            user_id=3001,
            platform="steam",
            game_id="620",
            game_title="Portal 2",
            target_price=10.00,
            last_price=32.99,
            currency="BRL",
            notify_on_any_sale=1,
            db_path=temp_db_path,
        )
        assert track_id > 0, "Falha ao inserir track"
        print(f"  ✓ add_track() executado com sucesso (ID gerado: {track_id}).")

        # Teste de consulta por usuário
        user_tracks = await db.get_user_tracks(3001, 2001, db_path=temp_db_path)
        assert len(user_tracks) == 1, "get_user_tracks retornou tamanho incorreto"
        assert user_tracks[0]["game_title"] == "Portal 2"
        print("  ✓ get_user_tracks() validado (1 jogo rastreado encontrado).")

        # Teste de consulta geral
        all_tracks = await db.get_all_tracks(db_path=temp_db_path)
        assert len(all_tracks) >= 1, "get_all_tracks vazio"
        print(f"  ✓ get_all_tracks() validado ({len(all_tracks)} registros totais).")

        # Teste de atualização de preço
        updated = await db.update_price("steam", "620", 15.00, db_path=temp_db_path)
        assert updated == 1, "update_price não atualizou registro"
        print("  ✓ update_price() atualizou o last_price para R$ 15,00.")

        # Teste de histórico e menor preço
        await db.record_price_history("620", "steam", 32.99, db_path=temp_db_path)
        await db.record_price_history("620", "steam", 6.59, db_path=temp_db_path)
        lowest = await db.get_lowest_historical_price("steam", "620", db_path=temp_db_path)
        assert lowest == 6.59, f"Menor preço histórico incorreto: {lowest}"
        print(f"  ✓ Histórico e get_lowest_historical_price() validado (Mínimo: R$ {lowest:.2f}).")

        # Teste de remoção
        removed = await db.remove_track(track_id, 3001, db_path=temp_db_path)
        assert removed is True, "Falha ao remover track"
        user_tracks_after = await db.get_user_tracks(3001, 2001, db_path=temp_db_path)
        assert len(user_tracks_after) == 0, "Registro ainda presente após remoção"
        print("  ✓ remove_track() validado com sucesso.")

    finally:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)

    # --------------------------------------------------------------------------
    # 2. Validação do Fetcher da Steam (steam.py)
    # --------------------------------------------------------------------------
    print("\n[2/3] 🎮 Testando API da Steam (steam.py)...")
    steam_query = "Portal 2"
    print(f"  • Buscando por '{steam_query}'...")
    steam_search = await steam.search_steam_games(steam_query, limit=3)
    assert len(steam_search) > 0, "Nenhum resultado retornado para Steam search"
    first_steam = steam_search[0]
    print(f"  ✓ Busca retornou: '{first_steam['name']}' (ID: {first_steam['id']})")

    steam_appid = first_steam["id"]
    steam_details = await steam.get_steam_game_details(steam_appid)
    assert steam_details is not None, "Falha ao obter detalhes da Steam"

    # Validação de campos críticos não-nulos
    assert steam_details.get("title"), "Campo 'title' ausente na Steam"
    assert steam_details.get("current_formatted"), "Campo 'current_formatted' ausente"
    assert steam_details.get("initial_formatted"), "Campo 'initial_formatted' ausente"
    assert steam_details.get("header_image"), "Campo 'header_image' ausente"
    assert steam_details.get("url"), "Campo 'url' ausente"

    print("  📋 Dados extraídos da Steam:")
    print(f"     - Título: {steam_details['title']}")
    print(f"     - Preço Regular: {steam_details['initial_formatted']}")
    print(f"     - Preço Atual: {steam_details['current_formatted']}")
    print(f"     - Desconto: {steam_details['discount_percent']}%")
    print(f"     - Banner: {steam_details['header_image']}")
    print(f"     - URL: {steam_details['url']}")

    # --------------------------------------------------------------------------
    # 3. Validação do Fetcher da Nintendo eShop (eshop.py)
    # --------------------------------------------------------------------------
    print("\n[3/3] 🔴 Testando API da Nintendo eShop (eshop.py)...")
    eshop_query = "Hollow Knight"
    print(f"  • Buscando por '{eshop_query}'...")
    eshop_search = await eshop.search_eshop_games(eshop_query, limit=3)
    assert len(eshop_search) > 0, "Nenhum resultado retornado para eShop search"
    first_eshop = eshop_search[0]
    print(f"  ✓ Busca retornou: '{first_eshop['name']}' (NSUID: {first_eshop['id']})")

    eshop_nsuid = first_eshop["id"]
    eshop_details = await eshop.get_eshop_game_details(eshop_nsuid, title_fallback=first_eshop["name"])
    assert eshop_details is not None, "Falha ao obter detalhes da eShop"

    # Validação de campos críticos não-nulos
    assert eshop_details.get("title"), "Campo 'title' ausente na eShop"
    assert eshop_details.get("current_formatted"), "Campo 'current_formatted' ausente"
    assert eshop_details.get("initial_formatted"), "Campo 'initial_formatted' ausente"
    assert eshop_details.get("header_image"), "Campo 'header_image' ausente"
    assert eshop_details.get("url"), "Campo 'url' ausente"

    print("  📋 Dados extraídos da Nintendo eShop:")
    print(f"     - Título: {eshop_details['title']}")
    print(f"     - Preço Regular: {eshop_details['initial_formatted']}")
    print(f"     - Preço Atual: {eshop_details['current_formatted']}")
    print(f"     - Desconto: {eshop_details['discount_percent']}%")
    print(f"     - Banner: {eshop_details['header_image']}")
    print(f"     - URL: {eshop_details['url']}")

    # Encerramento dos pools de teste
    await steam.close_http_client()
    await eshop.close_http_client()

    print("\n" + "=" * 70)
    print("🎉 TODOS OS TESTES PASSARAM! BACKEND E APIs 100% VALIDADOS.")
    print("=" * 70)


if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    except Exception as e:
        print(f"\n❌ ERRO NA VALIDAÇÃO: {e}")
        traceback.print_exc()
        sys.exit(1)
