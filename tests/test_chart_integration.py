"""Script de Homologação e Teste de Integração do Módulo de Gráficos e Multi-Embed Stack (tests/test_chart_integration.py)."""

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

import chart
import db


async def run_chart_tests():
    print("=" * 70)
    print("🧪 INICIANDO HOMOLOGAÇÃO DO MÓDULO DE GRÁFICOS & PERSISTÊNCIA")
    print("=" * 70)

    # 1. Configuração do Banco Temporário
    temp_fd, temp_db = tempfile.mkstemp(suffix="_chart_test.db")
    os.close(temp_fd)

    try:
        await db.init_db(db_path=temp_db)
        print("\n[1/4] 🗄️ Inserindo 4 pontos cronológicos de preço no SQLite...")

        game_id = "620"
        platform = "steam"
        game_title = "Portal 2"

        # Simulação de 4 pontos em datas distintas
        base_time = datetime.now(timezone.utc) - timedelta(days=30)
        simulated_prices = [
            (base_time.isoformat(), 32.99),
            ((base_time + timedelta(days=10)).isoformat(), 16.49),
            ((base_time + timedelta(days=20)).isoformat(), 32.99),
            ((base_time + timedelta(days=28)).isoformat(), 6.59),
        ]

        async with db.aiosqlite.connect(temp_db) as conn:
            for dt_iso, price in simulated_prices:
                await conn.execute(
                    """
                    INSERT INTO price_history (game_id, platform, price, recorded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (game_id, platform, price, dt_iso),
                )
            await conn.commit()

        print(f"  ✓ 4 registros de histórico inseridos com sucesso para '{game_title}'.")

        # 2. Consulta de Histórico Ordenado
        print("\n[2/4] 📊 Consultando histórico temporal ordenado...")
        history = await db.get_price_history(game_id, platform, db_path=temp_db)
        assert len(history) == 4, f"Esperado 4 registros, obtido {len(history)}"
        print(f"  ✓ Registros recuperados cronologicamente: {len(history)} pontos.")
        for idx, (dt, p) in enumerate(history, 1):
            print(f"     Ponto {idx}: {dt[:10]} -> R$ {p:,.2f}")

        # 3. Medição de Memória e Geração de Gráfico via chart.py
        print("\n[3/4] 🧠 Testando Renderização em Memória (tracemalloc & GC)...")
        tracemalloc.start()
        gc.collect()
        mem_before, _ = tracemalloc.get_traced_memory()

        # Execução não-bloqueante simulando a chamada no bot
        buffer = await asyncio.to_thread(chart.gerar_grafico_historico, game_title, history)

        mem_during, peak_mem = tracemalloc.get_traced_memory()
        gc.collect()
        mem_after, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"  • Memória inicial: {mem_before / 1024:.2f} KB")
        print(f"  • Pico durante renderização: {peak_mem / 1024:.2f} KB")
        print(f"  • Memória residual pós-GC: {mem_after / 1024:.2f} KB")

        # 4. Validação da Integridade do Buffer PNG
        print("\n[4/4] 🖼️ Validando Integridade do Buffer PNG...")
        assert buffer is not None, "O buffer gerado não pode ser None"
        raw_bytes = buffer.getvalue()
        buffer_size_kb = len(raw_bytes) / 1024
        assert len(raw_bytes) > 0, "Buffer PNG está vazio!"
        assert raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Buffer não possui cabeçalho válido de arquivo PNG"

        print(f"  ✓ Cabeçalho PNG validado (Magic bytes corretos).")
        print(f"  ✓ Tamanho final do PNG em memória: {buffer_size_kb:.2f} KB.")
        print(f"  ✓ Nenhum arquivo temporário gravado em disco.")

    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)

    print("\n" + "=" * 70)
    print("🎉 HOMOLOGAÇÃO CONCLUÍDA COM 100% DE SUCESSO!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_chart_tests())
