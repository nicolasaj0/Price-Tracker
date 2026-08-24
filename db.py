"""Módulo de Banco de Dados Assíncrono (aiosqlite) para PriceTracker.
Suporte Multi-Região, Notificações em DM (is_dm), Estatísticas de Telemetria e Online Backup API.
"""

from datetime import datetime, timezone
import io
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import aiosqlite

logger = logging.getLogger("PriceTracker.DB")

DB_PATH = os.getenv("DB_PATH", "bot_database.db")


def _ensure_parent_dir(db_path: str) -> None:
    """Garante a existência do diretório pai para caminhos personalizados ou volumes montados."""
    parent_dir = os.path.dirname(os.path.abspath(db_path))
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)
        logger.info("Diretório criado para o banco de dados: %s", parent_dir)


async def init_db(db_path: str = DB_PATH) -> None:
    """Inicializa as tabelas, executa migrações automáticas de schema e otimiza concorrência via WAL."""
    _ensure_parent_dir(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA busy_timeout = 5000;")
        await db.execute("PRAGMA synchronous = NORMAL;")

        # Criação de tabelas
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                game_id TEXT NOT NULL,
                game_title TEXT NOT NULL,
                target_price REAL,
                last_price REAL,
                currency TEXT DEFAULT 'BRL',
                country_code TEXT DEFAULT 'BR',
                notify_on_any_sale INTEGER DEFAULT 1,
                is_dm INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel_id, user_id, platform, game_id, country_code)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT DEFAULT 'BRL',
                country_code TEXT DEFAULT 'BR',
                recorded_at TEXT NOT NULL
            )
            """
        )

        # Migrações automáticas não-destrutivas
        try:
            await db.execute("ALTER TABLE tracked_games ADD COLUMN country_code TEXT DEFAULT 'BR';")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE tracked_games ADD COLUMN is_dm INTEGER DEFAULT 0;")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE price_history ADD COLUMN country_code TEXT DEFAULT 'BR';")
        except Exception:
            pass

        try:
            await db.execute("ALTER TABLE price_history ADD COLUMN currency TEXT DEFAULT 'BRL';")
        except Exception:
            pass

        # Índices otimizados
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracked_platform_game_country ON tracked_games(platform, game_id, country_code)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracked_user_channel ON tracked_games(user_id, channel_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracked_channel ON tracked_games(channel_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_history_game_region ON price_history(platform, game_id, country_code, recorded_at)"
        )
        await db.commit()
    logger.info("Banco SQLite pronto e otimizado com Multi-Região em %s", db_path)


# ==============================================================================
# MÓDULO DE BACKUP A QUENTE SEGURO (ONLINE BACKUP API)
# ==============================================================================

def _sync_create_backup(source_path: str) -> io.BytesIO:
    """Executa backup a quente síncrono consolidando WAL e SHM em buffer de memória."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Arquivo de banco de dados não encontrado: {source_path}")

    src_conn = sqlite3.connect(f"file:{os.path.abspath(source_path)}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(":memory:")

    try:
        src_conn.backup(dst_conn, pages=-1)
        binary_buffer = io.BytesIO(dst_conn.serialize())
        binary_buffer.seek(0)
        return binary_buffer
    finally:
        dst_conn.close()
        src_conn.close()


async def criar_backup_local(db_path: str = DB_PATH) -> io.BytesIO:
    """Gera um snapshot consistente do banco SQLite em memória RAM de forma não-bloqueante."""
    import asyncio
    return await asyncio.to_thread(_sync_create_backup, db_path)


async def get_database_stats(db_path: str = DB_PATH) -> Dict[str, int]:
    """Retorna estatísticas quantitativas do banco de dados para telemetria e diagnóstico."""
    _ensure_parent_dir(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute("SELECT COUNT(*) FROM tracked_games")
        total_tracks = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(DISTINCT game_id) FROM tracked_games")
        unique_games = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM tracked_games")
        unique_users = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM price_history")
        total_history = (await cursor.fetchone())[0]

        return {
            "total_tracked_games": total_tracks,
            "unique_games": unique_games,
            "unique_users": unique_users,
            "total_history_records": total_history,
        }


async def add_track(
    guild_id: Optional[int],
    channel_id: int,
    user_id: int,
    platform: str,
    game_id: str,
    game_title: str,
    target_price: Optional[float] = None,
    last_price: Optional[float] = None,
    currency: str = "BRL",
    country_code: str = "BR",
    notify_on_any_sale: int = 1,
    is_dm: int = 0,
    db_path: str = DB_PATH,
) -> int:
    """Adiciona ou atualiza um jogo monitorado isolado por região com opção de DM."""
    _ensure_parent_dir(db_path)
    cc = country_code.upper().strip()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            INSERT INTO tracked_games (
                guild_id, channel_id, user_id, platform, game_id, game_title, 
                target_price, last_price, currency, country_code, notify_on_any_sale, is_dm
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, user_id, platform, game_id, country_code) DO UPDATE SET
                target_price = excluded.target_price,
                last_price = excluded.last_price,
                game_title = excluded.game_title,
                currency = excluded.currency,
                notify_on_any_sale = excluded.notify_on_any_sale,
                is_dm = excluded.is_dm
            """,
            (
                guild_id,
                channel_id,
                user_id,
                platform.lower(),
                str(game_id),
                game_title[:250],
                target_price,
                last_price,
                currency.upper(),
                cc,
                notify_on_any_sale,
                1 if is_dm else 0,
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0


add_tracked_game = add_track


async def remove_track(
    track_id: int, user_id: Optional[int] = None, db_path: str = DB_PATH
) -> bool:
    """Remove um monitoramento pelo ID único."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        if user_id is not None:
            cursor = await db.execute(
                "DELETE FROM tracked_games WHERE id = ? AND user_id = ?",
                (track_id, user_id),
            )
        else:
            cursor = await db.execute(
                "DELETE FROM tracked_games WHERE id = ?",
                (track_id,),
            )
        await db.commit()
        return cursor.rowcount > 0


remove_tracked_game_by_id = remove_track


async def remove_tracked_game_exact(
    channel_id: int,
    user_id: int,
    platform: str,
    game_id: str,
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> bool:
    """Remove monitoramento exato por chave composta (canal, usuário, plataforma, id, região)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            DELETE FROM tracked_games 
            WHERE channel_id = ? AND user_id = ? AND platform = ? AND game_id = ? AND country_code = ?
            """,
            (channel_id, user_id, platform.lower(), str(game_id), country_code.upper()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def remove_tracks_by_channel(channel_id: int, db_path: str = DB_PATH) -> int:
    """Remove todos os alertas cadastrados em um canal deletado ou inacessível (Auto-Purge)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            "DELETE FROM tracked_games WHERE channel_id = ?",
            (channel_id,),
        )
        await db.commit()
        return cursor.rowcount


async def get_user_tracks(
    user_id: int, channel_id: Optional[int] = None, db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    """Retorna todos os jogos monitorados por um usuário específico."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        db.row_factory = aiosqlite.Row
        if channel_id is not None:
            cursor = await db.execute(
                """
                SELECT * FROM tracked_games 
                WHERE user_id = ? AND channel_id = ?
                ORDER BY id ASC
                """,
                (user_id, channel_id),
            )
        else:
            cursor = await db.execute(
                """
                SELECT * FROM tracked_games 
                WHERE user_id = ?
                ORDER BY id ASC
                """,
                (user_id,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


get_user_tracked_games = get_user_tracks


async def get_all_tracks(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retorna todos os monitoramentos cadastrados na base."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tracked_games ORDER BY id ASC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


get_all_tracked_games = get_all_tracks


async def get_user_tracked_games_paginated(
    user_id: int,
    channel_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 5,
    db_path: str = DB_PATH,
) -> Tuple[List[Dict[str, Any]], int]:
    """Retorna jogos monitorados com suporte à paginação."""
    offset = max(0, (page - 1) * page_size)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        db.row_factory = aiosqlite.Row
        if channel_id is not None:
            c_cursor = await db.execute(
                "SELECT COUNT(*) FROM tracked_games WHERE user_id = ? AND channel_id = ?",
                (user_id, channel_id),
            )
            data_cursor = await db.execute(
                """
                SELECT * FROM tracked_games 
                WHERE user_id = ? AND channel_id = ?
                ORDER BY id ASC LIMIT ? OFFSET ?
                """,
                (user_id, channel_id, page_size, offset),
            )
        else:
            c_cursor = await db.execute(
                "SELECT COUNT(*) FROM tracked_games WHERE user_id = ?",
                (user_id,),
            )
            data_cursor = await db.execute(
                """
                SELECT * FROM tracked_games 
                WHERE user_id = ?
                ORDER BY id ASC LIMIT ? OFFSET ?
                """,
                (user_id, page_size, offset),
            )

        total_row = await c_cursor.fetchone()
        total_count = total_row[0] if total_row else 0
        rows = await data_cursor.fetchall()
        return [dict(row) for row in rows], total_count


async def is_game_tracked_by_user(
    channel_id: int,
    user_id: int,
    platform: str,
    game_id: str,
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> bool:
    """Verifica se um jogo já está sendo monitorado pelo usuário no canal e país específico."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            SELECT 1 FROM tracked_games 
            WHERE channel_id = ? AND user_id = ? AND platform = ? AND game_id = ? AND country_code = ?
            LIMIT 1
            """,
            (channel_id, user_id, platform.lower(), str(game_id), country_code.upper()),
        )
        row = await cursor.fetchone()
        return row is not None


async def get_unique_tracked_games(db_path: str = DB_PATH) -> List[Dict[str, str]]:
    """Retorna lista única de jogos e regiões para varredura otimizada do worker periódico."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT DISTINCT platform, game_id, country_code, game_title 
            FROM tracked_games
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_subscribers_for_game(
    platform: str,
    game_id: str,
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> List[Dict[str, Any]]:
    """Retorna todos os canais e usuários inscritos para alertas de um jogo naquela região."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM tracked_games 
            WHERE platform = ? AND game_id = ? AND country_code = ?
            """,
            (platform.lower(), str(game_id), country_code.upper()),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_price(
    platform: str,
    game_id: str,
    new_price: float,
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> int:
    """Atualiza o last_price para todas as instâncias ativas do jogo naquela região."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            UPDATE tracked_games
            SET last_price = ?
            WHERE platform = ? AND game_id = ? AND country_code = ?
            """,
            (new_price, platform.lower(), str(game_id), country_code.upper()),
        )
        await db.commit()
        return cursor.rowcount


update_last_price_for_game = update_price


async def record_price_history(
    game_id: str,
    platform: str,
    price: float,
    currency: str = "BRL",
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> bool:
    """Registra uma entrada no histórico temporal de preços (ISO 8601) com isolamento regional."""
    if price <= 0:
        return False

    _ensure_parent_dir(db_path)
    cc = country_code.upper().strip()
    curr = currency.upper().strip()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            SELECT price FROM price_history
            WHERE game_id = ? AND platform = ? AND country_code = ?
            ORDER BY id DESC LIMIT 1
            """,
            (str(game_id), platform.lower(), cc),
        )
        last_row = await cursor.fetchone()
        if last_row and abs(last_row[0] - price) < 0.001:
            return False

        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO price_history (game_id, platform, price, currency, country_code, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(game_id), platform.lower(), price, curr, cc, now_iso),
        )
        await db.commit()
        return True


async def get_price_history(
    game_id: str,
    platform: str,
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> List[Tuple[str, float]]:
    """Recupera os registros cronológicos de preço de um jogo filtrados pela região."""
    cc = country_code.upper().strip()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            SELECT recorded_at, price FROM price_history
            WHERE game_id = ? AND platform = ? AND country_code = ?
            ORDER BY id ASC
            """,
            (str(game_id), platform.lower(), cc),
        )
        rows = await cursor.fetchall()
        return [(str(row[0]), float(row[1])) for row in rows]


async def get_lowest_historical_price(
    platform: str,
    game_id: str,
    country_code: str = "BR",
    db_path: str = DB_PATH,
) -> Optional[float]:
    """Retorna o menor preço histórico já registrado para o jogo na região especificada."""
    cc = country_code.upper().strip()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000;")
        cursor = await db.execute(
            """
            SELECT MIN(price) FROM price_history
            WHERE platform = ? AND game_id = ? AND country_code = ? AND price > 0
            """,
            (platform.lower(), str(game_id), cc),
        )
        row = await cursor.fetchone()
        return float(row[0]) if (row and row[0] is not None) else None
