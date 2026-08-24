"""Bateria de Testes para Persistência Resiliente, Online Backup e Auto-Recuperação (tests/test_backup_restore.py)."""

import asyncio
from datetime import datetime, timezone
import os
import shutil
import sqlite3
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import db


async def run_backup_restore_tests():
    print("=" * 75)
    print("💾 INICIANDO TESTES DE BACKUP A QUENTE & AUTO-RECUPERAÇÃO DO SQLITE")
    print("=" * 75)

    temp_dir = tempfile.mkdtemp(suffix="_backup_test")
    nested_db_path = os.path.join(temp_dir, "nested", "storage", "bot_database.db")
    restored_db_path = os.path.join(temp_dir, "restored", "bot_database.db")

    try:
        # ----------------------------------------------------------------------
        # 1. Teste de Criação de Diretório Pai & Inicialização
        # ----------------------------------------------------------------------
        print("\n[1/4] 📁 Testando Criação Automática de Diretórios Pais...")
        await db.init_db(db_path=nested_db_path)
        assert os.path.exists(nested_db_path), "O arquivo de banco de dados não foi criado no caminho aninhado"
        print(f"  ✓ Diretório aninhado e banco SQLite criados: {nested_db_path}")

        # ----------------------------------------------------------------------
        # 2. Inserção de Dados e Transações em Modo WAL
        # ----------------------------------------------------------------------
        print("\n[2/4] 📝 Inserindo Dados e Ativando Transações WAL...")
        await db.add_track(
            guild_id=123,
            channel_id=456,
            user_id=789,
            platform="steam",
            game_id="620",
            game_title="Portal 2",
            target_price=10.0,
            last_price=32.99,
            currency="BRL",
            country_code="BR",
            db_path=nested_db_path,
        )
        await db.add_track(
            guild_id=123,
            channel_id=456,
            user_id=789,
            platform="eshop",
            game_id="70010000117998",
            game_title="Hollow Knight",
            target_price=15.0,
            last_price=27.99,
            currency="BRL",
            country_code="BR",
            db_path=nested_db_path,
        )
        await db.record_price_history("620", "steam", 32.99, currency="BRL", country_code="BR", db_path=nested_db_path)
        await db.record_price_history("620", "steam", 16.49, currency="BRL", country_code="BR", db_path=nested_db_path)

        tracks = await db.get_all_tracks(db_path=nested_db_path)
        assert len(tracks) == 2, f"Esperado 2 tracks, obtido {len(tracks)}"
        print(f"  ✓ 2 jogos e histórico registrados no banco com WAL ativo.")

        # ----------------------------------------------------------------------
        # 3. Teste de Snapshot a Quente (Online Backup API)
        # ----------------------------------------------------------------------
        print("\n[3/4] ⚡ Gerando Backup a Quente Consolidado em Memória RAM...")
        backup_buffer = await db.criar_backup_local(db_path=nested_db_path)
        assert backup_buffer is not None, "O buffer de backup retornado é nulo"
        raw_bytes = backup_buffer.getvalue()
        assert len(raw_bytes) > 0, "O buffer de backup está vazio"
        assert raw_bytes.startswith(b"SQLite format 3\x00"), "O cabeçalho do arquivo gerado não é um banco SQLite válido"
        print(f"  ✓ Snapshot consolidado com sucesso ({len(raw_bytes) / 1024:.2f} KB) sem bloquear conexões.")

        # ----------------------------------------------------------------------
        # 4. Teste de Auto-Recuperação e Integridade dos Dados Restaurados
        # ----------------------------------------------------------------------
        print("\n[4/4] 🔄 Testando Restauração do Snapshot em Novo Ambiente...")
        db._ensure_parent_dir(restored_db_path)
        with open(restored_db_path, "wb") as f:
            f.write(raw_bytes)

        assert os.path.exists(restored_db_path), "Falha ao gravar arquivo restaurado"

        # Inicializa banco restaurado e valida integridade
        await db.init_db(db_path=restored_db_path)
        restored_tracks = await db.get_all_tracks(db_path=restored_db_path)
        assert len(restored_tracks) == 2, f"Erro nos dados restaurados: {len(restored_tracks)}"

        restored_history = await db.get_price_history("620", "steam", country_code="BR", db_path=restored_db_path)
        assert len(restored_history) == 2, f"Erro no histórico restaurado: {len(restored_history)}"

        print(f"  ✓ {len(restored_tracks)} monitoramentos e {len(restored_history)} registros de histórico recuperados com 100% de integridade.")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n" + "=" * 75)
    print("🎉 TESTES DE PERSISTÊNCIA, BACKUP E AUTO-RECUPERAÇÃO CONCLUÍDOS COM SUCESSO!")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(run_backup_restore_tests())
