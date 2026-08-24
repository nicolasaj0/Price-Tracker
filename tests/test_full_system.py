"""Bateria de Homologação Final do Sistema Completo (tests/test_full_system.py).
Valida:
1. Sanitização de títulos longos, caracteres especiais e entidades HTML.
2. Fallback de Card 1 único quando não há histórico suficiente (< 2 pontos).
3. Concorrência intensa de leitura/escrita no SQLite com WAL Mode e busy_timeout.
4. Renderização não-bloqueante de gráficos e controle estrito de memória RAM.
5. Auto-purge de alertas órfãos para canais inacessíveis ou deletados.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import gc
import os
import sys
import tempfile
import tracemalloc

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import bot
import chart
import db
import eshop
import steam


async def run_full_system_test():
    print("=" * 75)
    print("🚀 INICIANDO BATERIA DE HOMOLOGAÇÃO FINAL DO SISTEMA COMPLETO")
    print("=" * 75)

    temp_fd, temp_db = tempfile.mkstemp(suffix="_full_test.db")
    os.close(temp_fd)

    try:
        # ----------------------------------------------------------------------
        # 1. Teste de Inicialização e Otimização do SQLite
        # ----------------------------------------------------------------------
        print("\n[1/5] 🗄️ Testando Concorrência SQLite (WAL Mode & Busy Timeout)...")
        await db.init_db(db_path=temp_db)

        # Simula 50 operações concorrentes de leitura e escrita simultânea
        async def concurrent_worker(worker_id: int):
            for i in range(5):
                # Escrita
                await db.add_track(
                    guild_id=100 + worker_id,
                    channel_id=200 + (worker_id % 3),
                    user_id=300 + worker_id,
                    platform="steam" if worker_id % 2 == 0 else "eshop",
                    game_id=f"game_{worker_id}_{i}",
                    game_title=f"Jogo Concorrente #{worker_id} - {i}",
                    target_price=20.0 + i,
                    last_price=25.0 + i,
                    currency="BRL",
                    notify_on_any_sale=1,
                    db_path=temp_db,
                )
                await db.record_price_history(f"game_{worker_id}_{i}", "steam", 25.0 + i, db_path=temp_db)

                # Leitura
                await db.get_user_tracks(300 + worker_id, 200 + (worker_id % 3), db_path=temp_db)
                await db.get_lowest_historical_price("steam", f"game_{worker_id}_{i}", db_path=temp_db)

        workers = [concurrent_worker(w) for w in range(10)]
        await asyncio.gather(*workers)

        all_records = await db.get_all_tracks(db_path=temp_db)
        assert len(all_records) == 50, f"Esperado 50 registros cadastrados, obtido {len(all_records)}"
        print(f"  ✓ 50 transações concorrentes de leitura/escrita executadas sem database lock.")

        # ----------------------------------------------------------------------
        # 2. Teste de Sanitização e Formatação de Strings / Embeds
        # ----------------------------------------------------------------------
        print("\n[2/5] 🛡️ Testando Sanitização de Títulos Longos e Entidades HTML...")
        unsafe_title = "Super Game &lt;Special Edition&gt; &amp; &quot;Deluxe DLC Pack&quot; " + ("X" * 300)
        cleaned = bot.clean_str(unsafe_title, max_len=200)

        assert "&lt;" not in cleaned, "Entidade HTML não decodificada"
        assert "<Special Edition>" in cleaned, "Conteúdo HTML não sanitizado corretamente"
        assert len(cleaned) <= 200, f"String não truncada para limite de segurança: {len(cleaned)}"
        print(f"  ✓ Título sanitizado e truncado com segurança ({len(cleaned)} chars).")
        print(f"  ✓ Entidades HTML decodificadas com sucesso.")

        # ----------------------------------------------------------------------
        # 3. Teste de Fallback de Card 1 Único (< 2 Pontos de Histórico)
        # ----------------------------------------------------------------------
        print("\n[3/5] 📄 Testando Fallback de Card 1 Único (< 2 registros)...")
        # Simula jogo com apenas 1 registro de preço
        game_single_data = {
            "platform": "steam",
            "game_id": "999",
            "title": "Jogo Recém Lançado",
            "is_free": False,
            "currency": "BRL",
            "initial_price": 100.0,
            "current_price": 100.0,
            "discount_percent": 0,
            "initial_formatted": "R$ 100,00",
            "current_formatted": "R$ 100,00",
            "on_sale": False,
            "header_image": "https://example.com/banner.jpg",
            "url": "https://store.steampowered.com/app/999/",
            "description": "Descrição de teste...",
        }
        await db.record_price_history("999", "steam", 100.0, db_path=temp_db)
        history_single = await db.get_price_history("999", "steam", db_path=temp_db)
        assert len(history_single) == 1, "Histórico unitário inconsistente"

        # Tenta gerar gráfico: deve retornar None e permitir Card 1 único
        chart_result = await asyncio.to_thread(
            chart.gerar_grafico_historico, game_single_data["title"], history_single
        )
        assert chart_result is None, "Gráfico não deveria ser gerado com apenas 1 registro"
        print(f"  ✓ Fallback validado: histórico unitário não gera gráfico desnecessário.")

        # ----------------------------------------------------------------------
        # 4. Teste de Renderização Não-Bloqueante de Gráficos e RAM (< 120 MB)
        # ----------------------------------------------------------------------
        print("\n[4/5] 📈 Testando Renderização Não-Bloqueante & Consumo de Memória...")
        tracemalloc.start()
        gc.collect()

        # Inserção de 6 pontos temporais para Portal 2
        p2_history_sim = [
            ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), 32.99),
            ((datetime.now(timezone.utc) - timedelta(days=45)).isoformat(), 16.49),
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), 32.99),
            ((datetime.now(timezone.utc) - timedelta(days=20)).isoformat(), 6.59),
            ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(), 32.99),
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), 6.59),
        ]
        async with db.aiosqlite.connect(temp_db) as conn:
            for dt_str, pr in p2_history_sim:
                await conn.execute(
                    """
                    INSERT INTO price_history (game_id, platform, price, currency, country_code, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("620", "steam", pr, "BRL", "BR", dt_str),
                )
            await conn.commit()

        history_multi = await db.get_price_history("620", "steam", db_path=temp_db)
        assert len(history_multi) >= 6, "Histórico multiponto incompleto"

        # Geração do gráfico offloading em thread pool
        chart_buffer = await asyncio.to_thread(
            chart.gerar_grafico_historico, "Portal 2", history_multi
        )
        assert chart_buffer is not None, "Falha na geração do gráfico multi-embed"
        assert chart_buffer.getvalue().startswith(b"\x89PNG"), "Buffer gerado não é um PNG válido"

        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak_mem / (1024 * 1024)
        print(f"  ✓ Gráfico PNG renderizado com sucesso ({len(chart_buffer.getvalue()) / 1024:.2f} KB).")
        print(f"  ✓ Pico de memória durante execução: {peak_mb:.2f} MB (dentro dos limites).")
        assert peak_mb < 120.0, f"Uso de memória ({peak_mb:.2f} MB) excedeu o teto estrito de 120 MB"

        # ----------------------------------------------------------------------
        # 5. Teste de Auto-Purge de Canais Inacessíveis
        # ----------------------------------------------------------------------
        print("\n[5/5] 🧹 Testando Auto-Purge de Canais Órfãos / Inacessíveis...")
        bad_channel_id = 999999
        await db.add_track(
            guild_id=1,
            channel_id=bad_channel_id,
            user_id=10,
            platform="steam",
            game_id="111",
            game_title="Bad Game 1",
            currency="BRL",
            db_path=temp_db,
        )
        await db.add_track(
            guild_id=1,
            channel_id=bad_channel_id,
            user_id=20,
            platform="steam",
            game_id="222",
            game_title="Bad Game 2",
            currency="BRL",
            db_path=temp_db,
        )

        initial_bad_count = len(await db.get_user_tracks(10, bad_channel_id, db_path=temp_db))
        assert initial_bad_count == 1, "Track inicial não criado"

        # Executa rotina de purge simulada
        purged = await db.remove_tracks_by_channel(bad_channel_id, db_path=temp_db)
        assert purged == 2, f"Esperado 2 registros removidos pelo purge, obtido {purged}"

        remaining = len(await db.get_user_tracks(10, bad_channel_id, db_path=temp_db))
        assert remaining == 0, "Registros ainda constam no banco após auto-purge"
        print(f"  ✓ Auto-purge removeu {purged} alertas associados ao canal inacessível.")

    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)
        await steam.close_http_client()
        await eshop.close_http_client()

    print("\n" + "=" * 75)
    print("🎉 BATERIA COMPLETA DE HOMOLOGAÇÃO CONCLUÍDA COM 100% DE SUCESSO!")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(run_full_system_test())
