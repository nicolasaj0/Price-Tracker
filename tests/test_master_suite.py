"""Suíte Mestra de Homologação e Testes Pré-Lançamento (tests/test_master_suite.py).
Executa toda a bateria de testes em sequência estrita para validação final de sanidade e Code Freeze.
"""

import asyncio
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
TESTS_DIR = os.path.dirname(__file__)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import test_backup_restore
import test_full_system
import test_itad_integration
import test_multiregion
import test_v1_1_features


async def main():
    print("=" * 80)
    print("🛸 SUÍTE MESTRA DE AUDITORIA & HOMOLOGAÇÃO PRÉ-LANÇAMENTO (PRICE TRACKER v1.1)")
    print("=" * 80)

    start_time = time.monotonic()

    print("\n>>> ETAPA 1/5: VALIDAÇÃO DO SISTEMA CENTRAL & RESILIÊNCIA DE PRODUÇÃO")
    await test_full_system.run_full_system_test()

    print("\n>>> ETAPA 2/5: VALIDAÇÃO DO SUPORTE GLOBAL MULTI-REGIÃO & CÂMBIO")
    await test_multiregion.run_multiregion_tests()

    print("\n>>> ETAPA 3/5: VALIDAÇÃO DE PERSISTÊNCIA, BACKUP A QUENTE & AUTO-RECUPERAÇÃO")
    await test_backup_restore.run_backup_restore_tests()

    print("\n>>> ETAPA 4/5: VALIDAÇÃO DOS RECURSOS v1.1 (CACHE, DM, /COMPARAR, /STATUS)")
    await test_v1_1_features.run_v1_1_tests()

    print("\n>>> ETAPA 5/5: VALIDAÇÃO DA INTEGRAÇÃO COM IsThereAnyDeal (ITAD v2)")
    await test_itad_integration.run_itad_tests()

    elapsed = time.monotonic() - start_time
    print("\n" + "=" * 80)
    print(f"🏆 TODAS AS 5 SUÍTES DE HOMOLOGAÇÃO FORAM CONCLUÍDAS COM SUCESSO EM {elapsed:.2f}s!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
