"""Script de Homologação e Teste de Suporte Multi-Região Global (tests/test_multiregion.py)."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import bot
import db
import eshop
import steam


async def run_multiregion_tests():
    print("=" * 75)
    print("🌍 INICIANDO HOMOLOGAÇÃO DO SUPORTE GLOBAL MULTI-REGIÃO")
    print("=" * 75)

    temp_fd, temp_db = tempfile.mkstemp(suffix="_multiregion_test.db")
    os.close(temp_fd)

    try:
        # ----------------------------------------------------------------------
        # 1. Teste de Banco de Dados com Isolamento Regional
        # ----------------------------------------------------------------------
        print("\n[1/4] 🗄️ Testando Persistência com Isolamento de Moedas e Regiões...")
        await db.init_db(db_path=temp_db)

        # Inserção de monitoramentos para o mesmo jogo em regiões distintas
        id_br = await db.add_track(
            guild_id=100,
            channel_id=200,
            user_id=300,
            platform="steam",
            game_id="620",
            game_title="Portal 2",
            target_price=10.0,
            last_price=32.99,
            currency="BRL",
            country_code="BR",
            db_path=temp_db,
        )
        id_us = await db.add_track(
            guild_id=100,
            channel_id=200,
            user_id=300,
            platform="steam",
            game_id="620",
            game_title="Portal 2",
            target_price=5.0,
            last_price=9.99,
            currency="USD",
            country_code="US",
            db_path=temp_db,
        )
        assert id_br != id_us, "Falha na chave composta por região"
        print("  ✓ Tracks cadastrados com sucesso para BR (BRL) e US (USD) sem colisão.")

        # Inserção de histórico isolado
        t1 = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        t2 = datetime.now(timezone.utc).isoformat()

        await db.record_price_history("620", "steam", 32.99, currency="BRL", country_code="BR", db_path=temp_db)
        await db.record_price_history("620", "steam", 6.59, currency="BRL", country_code="BR", db_path=temp_db)

        await db.record_price_history("620", "steam", 9.99, currency="USD", country_code="US", db_path=temp_db)
        await db.record_price_history("620", "steam", 1.99, currency="USD", country_code="US", db_path=temp_db)

        history_br = await db.get_price_history("620", "steam", country_code="BR", db_path=temp_db)
        history_us = await db.get_price_history("620", "steam", country_code="US", db_path=temp_db)

        assert len(history_br) == 2, f"Histórico BR incorreto: {len(history_br)}"
        assert len(history_us) == 2, f"Histórico US incorreto: {len(history_us)}"

        low_br = await db.get_lowest_historical_price("steam", "620", country_code="BR", db_path=temp_db)
        low_us = await db.get_lowest_historical_price("steam", "620", country_code="US", db_path=temp_db)

        assert low_br == 6.59, f"Menor BR incorreto: {low_br}"
        assert low_us == 1.99, f"Menor US incorreto: {low_us}"
        print(f"  ✓ Menor histórico isolado com sucesso: BR = R$ {low_br:.2f} | US = ${low_us:.2f}")

        # ----------------------------------------------------------------------
        # 2. Teste da API da Steam Multi-Região
        # ----------------------------------------------------------------------
        print("\n[2/4] 🎮 Testando API da Steam Multi-Região (BR, US, GB, JP)...")
        steam_br = await steam.get_steam_game_details("620", country_code="BR")
        assert steam_br and steam_br["currency"] == "BRL"
        print(f"  ✓ Steam BR: {steam_br['title']} -> {steam_br['current_formatted']} ({steam_br['currency']})")

        steam_us = await steam.get_steam_game_details("620", country_code="US")
        assert steam_us and steam_us["currency"] == "USD"
        print(f"  ✓ Steam US: {steam_us['title']} -> {steam_us['current_formatted']} ({steam_us['currency']})")

        steam_gb = await steam.get_steam_game_details("620", country_code="GB")
        assert steam_gb and steam_gb["currency"] == "GBP"
        print(f"  ✓ Steam GB: {steam_gb['title']} -> {steam_gb['current_formatted']} ({steam_gb['currency']})")

        # ----------------------------------------------------------------------
        # 3. Teste da API da Nintendo eShop Multi-Região
        # ----------------------------------------------------------------------
        print("\n[3/4] 🔴 Testando API da Nintendo eShop Multi-Região (BR, US)...")
        eshop_br = await eshop.get_eshop_game_details("70010000117998", country_code="BR", title_fallback="Hollow Knight")
        assert eshop_br and eshop_br["currency"] == "BRL"
        print(f"  ✓ eShop BR: {eshop_br['title']} -> {eshop_br['current_formatted']} ({eshop_br['currency']})")

        eshop_us = await eshop.get_eshop_price_by_nsuid("70010000117998", country_code="US")
        assert eshop_us and eshop_us["currency"] == "USD"
        print(f"  ✓ eShop US: Hollow Knight -> {eshop_us['current_formatted']} ({eshop_us['currency']})")

        # ----------------------------------------------------------------------
        # 4. Teste de Formatação Monetária Multi-Moeda
        # ----------------------------------------------------------------------
        print("\n[4/4] 💱 Testando Formatação Monetária Multi-Moeda...")
        fmt_us = steam.format_currency_global(9.99, "USD", "US")
        assert "$" in fmt_us
        print(f"  ✓ Formatação em USD validada: {fmt_us}")

        fmt_br = steam.format_currency_global(32.99, "BRL", "BR")
        assert "R$" in fmt_br
        print(f"  ✓ Formatação em BRL validada: {fmt_br}")

        fmt_pt = steam.format_currency_global(9.75, "EUR", "PT")
        assert "€" in fmt_pt
        print(f"  ✓ Formatação em EUR validada: {fmt_pt}")

        fmt_jp = steam.format_currency_global(1200.0, "JPY", "JP")
        assert "¥" in fmt_jp
        print(f"  ✓ Formatação em JPY validada: {fmt_jp}")

    finally:
        if os.path.exists(temp_db):
            os.remove(temp_db)
        await steam.close_http_client()
        await eshop.close_http_client()

    print("\n" + "=" * 75)
    print("🎉 HOMOLOGAÇÃO MULTI-REGIÃO CONCLUÍDA COM 100% DE SUCESSO!")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(run_multiregion_tests())
