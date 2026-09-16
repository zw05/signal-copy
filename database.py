import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = 'stock_msg.db'


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema + migrations
#
# Each entry is (version, [sql statements]). Applied in order, once. Never edit
# an entry that has shipped; add a new version instead.
# ---------------------------------------------------------------------------

MIGRATIONS = [
    (1, [
        '''
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id      TEXT NOT NULL UNIQUE,
            guild_id        TEXT,
            channel_id      TEXT NOT NULL,
            author_id       TEXT NOT NULL,
            author_name     TEXT,
            content         TEXT NOT NULL,
            reply_to_id     TEXT,
            created_at      TEXT NOT NULL,
            ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
            parse_status    TEXT NOT NULL DEFAULT 'pending'
        )
        ''',
        'CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_messages_parse_status ON messages(parse_status)',
        '''
        CREATE TABLE IF NOT EXISTS option_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      INTEGER NOT NULL REFERENCES messages(id),
            ticker          TEXT NOT NULL,
            option_type     TEXT NOT NULL CHECK (option_type IN ('call', 'put')),
            strike          REAL,
            expiration      TEXT,
            action          TEXT CHECK (action IN ('buy', 'sell', 'close', 'unknown')),
            contracts       INTEGER,
            limit_price     REAL,
            raw_excerpt     TEXT,
            confidence      REAL,
            parser_version  TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            trade_status    TEXT NOT NULL DEFAULT 'pending'
        )
        ''',
        'CREATE INDEX IF NOT EXISTS idx_signals_trade_status ON option_signals(trade_status)',
        'CREATE INDEX IF NOT EXISTS idx_signals_ticker ON option_signals(ticker)',
        '''
        CREATE TABLE IF NOT EXISTS paper_trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       INTEGER NOT NULL REFERENCES option_signals(id),
            broker_order_id TEXT,
            side            TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            fill_price      REAL,
            status          TEXT NOT NULL DEFAULT 'pending',
            error_message   TEXT,
            submitted_at    TEXT,
            filled_at       TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        ''',
    ]),
    # Phase 2: exit/update signals, position lifecycle, labeling queue.
    (2, [
        'ALTER TABLE messages ADD COLUMN attachment_urls TEXT',
        'ALTER TABLE option_signals ADD COLUMN avg_price REAL',
        'ALTER TABLE option_signals ADD COLUMN underlying_pt REAL',
        'ALTER TABLE option_signals ADD COLUMN expiration_inferred INTEGER NOT NULL DEFAULT 0',
        "ALTER TABLE option_signals ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'open'",
        'ALTER TABLE option_signals ADD COLUMN author_id TEXT',
        'ALTER TABLE option_signals ADD COLUMN signal_created_at TEXT',
        'CREATE INDEX IF NOT EXISTS idx_signals_lifecycle ON option_signals(lifecycle_status)',
        '''
        CREATE TABLE IF NOT EXISTS signal_updates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       INTEGER NOT NULL REFERENCES option_signals(id),
            message_id      INTEGER NOT NULL REFERENCES messages(id),
            action          TEXT NOT NULL CHECK (action IN
                              ('price_update', 'trim', 'close', 'stop', 'add', 'note')),
            fraction        REAL,
            contracts       INTEGER,
            price           REAL,
            pct_gain        REAL,
            stop_price      REAL,
            stop_pct        REAL,
            confidence      REAL,
            source          TEXT NOT NULL CHECK (source IN ('regex', 'llm', 'manual')),
            attach_rule     TEXT,
            raw_excerpt     TEXT,
            parser_version  TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (message_id)
        )
        ''',
        'CREATE INDEX IF NOT EXISTS idx_updates_signal ON signal_updates(signal_id)',
        '''
        CREATE TABLE IF NOT EXISTS needs_label (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      INTEGER NOT NULL REFERENCES messages(id) UNIQUE,
            reason          TEXT NOT NULL,
            candidate       TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at     TEXT
        )
        ''',
    ]),
    # Phase 3: execution. positions/orders replace paper_trades.
    (3, [
        "ALTER TABLE signal_updates ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'pending'",
        'ALTER TABLE signal_updates ADD COLUMN executed_at TEXT',
        'ALTER TABLE signal_updates ADD COLUMN execution_note TEXT',
        'CREATE INDEX IF NOT EXISTS idx_updates_exec ON signal_updates(execution_status)',
        '''
        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       INTEGER NOT NULL UNIQUE REFERENCES option_signals(id),
            broker          TEXT NOT NULL,
            contract_symbol TEXT NOT NULL,
            qty_target      INTEGER NOT NULL,
            qty_opened      INTEGER NOT NULL DEFAULT 0,
            remaining_qty   INTEGER NOT NULL DEFAULT 0,
            avg_cost        REAL,
            realized_pnl    REAL NOT NULL DEFAULT 0,
            stop_price      REAL,
            trimmed         INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'opening'
                            CHECK (status IN ('opening','open','closed','missed','error')),
            exit_reason     TEXT,
            entry_bid       REAL, entry_ask REAL, entry_delta REAL, entry_iv REAL,
            source_entry    REAL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            opened_at       TEXT,
            closed_at       TEXT
        )
        ''',
        'CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)',
        '''
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id     INTEGER NOT NULL REFERENCES positions(id),
            update_id       INTEGER REFERENCES signal_updates(id),
            broker          TEXT NOT NULL,
            broker_order_id TEXT,
            client_order_id TEXT,
            side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
            reason          TEXT NOT NULL,
            qty             INTEGER NOT NULL,
            limit_price     REAL,
            status          TEXT NOT NULL DEFAULT 'new',
            filled_qty      INTEGER NOT NULL DEFAULT 0,
            fill_price      REAL,
            error           TEXT,
            submitted_at    TEXT,
            filled_at       TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        ''',
        'CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)',
        'CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id)',
    ]),
]


def init_db():
    with get_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        ''')
        applied = {
            row['version']
            for row in conn.execute('SELECT version FROM schema_version')
        }
        # Pre-migration databases already have the v1 tables; the v1
        # statements are all IF NOT EXISTS so re-running them is harmless.
        for version, statements in MIGRATIONS:
            if version in applied:
                continue
            for sql in statements:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    # ALTER TABLE ADD COLUMN on a column that already exists
                    if 'duplicate column name' not in str(exc):
                        raise
            conn.execute('INSERT INTO schema_version (version) VALUES (?)', (version,))


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def insert_message(discord_id, guild_id, channel_id, author_id, author_name,
                   content, reply_to_id, created_at, attachment_urls=None):
    with get_connection() as conn:
        cursor = conn.execute('''
            INSERT OR IGNORE INTO messages
                (discord_id, guild_id, channel_id, author_id, author_name,
                 content, reply_to_id, created_at, attachment_urls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (str(discord_id), str(guild_id) if guild_id else None, str(channel_id),
              str(author_id), author_name, content,
              str(reply_to_id) if reply_to_id else None, created_at,
              attachment_urls))
        if cursor.rowcount:
            return cursor.lastrowid, True
        row = conn.execute(
            'SELECT id FROM messages WHERE discord_id = ?',
            (str(discord_id),),
        ).fetchone()
        return (row['id'], False) if row else (None, False)


def get_message(message_id):
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM messages WHERE id = ?', (message_id,),
        ).fetchone()
        return dict(row) if row else None


def get_message_by_discord_id(discord_id):
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM messages WHERE discord_id = ?', (str(discord_id),),
        ).fetchone()
        return dict(row) if row else None


def get_messages(limit=10):
    with get_connection() as conn:
        rows = conn.execute('''
            SELECT author_name, content, channel_id, created_at, parse_status
            FROM messages
            ORDER BY id DESC
            LIMIT ?
        ''', (limit,)).fetchall()
        return [dict(row) for row in rows]


def get_all_messages():
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT * FROM messages ORDER BY created_at ASC, id ASC',
        ).fetchall()
        return [dict(row) for row in rows]


def get_unparsed_messages(limit=10):
    with get_connection() as conn:
        rows = conn.execute('''
            SELECT id, discord_id, content, reply_to_id, created_at
            FROM messages
            WHERE parse_status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
        ''', (limit,)).fetchall()
        return [dict(row) for row in rows]


def update_message_parse_status(message_id, status):
    with get_connection() as conn:
        conn.execute(
            'UPDATE messages SET parse_status = ? WHERE id = ?',
            (status, message_id),
        )


# ---------------------------------------------------------------------------
# option_signals (entries)
# ---------------------------------------------------------------------------

def insert_signal(message_id, ticker, option_type, strike, expiration, action,
                  contracts, limit_price, raw_excerpt, confidence, parser_version,
                  avg_price=None, underlying_pt=None, expiration_inferred=0,
                  author_id=None, signal_created_at=None):
    with get_connection() as conn:
        cursor = conn.execute('''
            INSERT INTO option_signals
                (message_id, ticker, option_type, strike, expiration, action,
                 contracts, limit_price, raw_excerpt, confidence, parser_version,
                 avg_price, underlying_pt, expiration_inferred, author_id,
                 signal_created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (message_id, ticker, option_type, strike, expiration, action,
              contracts, limit_price, raw_excerpt, confidence, parser_version,
              avg_price, underlying_pt, expiration_inferred, author_id,
              signal_created_at))
        return cursor.lastrowid


def get_signal(signal_id):
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM option_signals WHERE id = ?', (signal_id,),
        ).fetchone()
        return dict(row) if row else None


def get_signal_by_message_id(message_id):
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM option_signals WHERE message_id = ?', (message_id,),
        ).fetchone()
        return dict(row) if row else None


def get_open_signals(author_id=None, as_of=None):
    """Signals that are still 'open': not closed and not past expiration.

    `as_of` is an ISO timestamp; defaults to now. Passed explicitly by the
    replay tool so historical messages see the world as it was.
    """
    as_of = as_of or datetime.utcnow().isoformat()
    as_of_date = as_of[:10]
    with get_connection() as conn:
        sql = '''
            SELECT s.*, m.discord_id AS message_discord_id
            FROM option_signals s
            JOIN messages m ON m.id = s.message_id
            WHERE s.lifecycle_status = 'open'
              AND COALESCE(s.signal_created_at, s.created_at) <= ?
              AND (s.expiration IS NULL OR s.expiration >= ?)
        '''
        params = [as_of, as_of_date]
        if author_id is not None:
            sql += ' AND s.author_id = ?'
            params.append(str(author_id))
        sql += ' ORDER BY COALESCE(s.signal_created_at, s.created_at) DESC'
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def update_signal_lifecycle(signal_id, status):
    with get_connection() as conn:
        conn.execute(
            'UPDATE option_signals SET lifecycle_status = ? WHERE id = ?',
            (status, signal_id),
        )


def get_pending_signals(limit=10):
    with get_connection() as conn:
        rows = conn.execute('''
            SELECT s.id, s.message_id, s.ticker, s.option_type, s.strike,
                   s.expiration, s.action, s.contracts, s.limit_price,
                   s.raw_excerpt, s.confidence, m.content AS message_content
            FROM option_signals s
            JOIN messages m ON m.id = s.message_id
            WHERE s.trade_status = 'pending'
            ORDER BY s.created_at ASC
            LIMIT ?
        ''', (limit,)).fetchall()
        return [dict(row) for row in rows]


def update_signal_trade_status(signal_id, status):
    with get_connection() as conn:
        conn.execute(
            'UPDATE option_signals SET trade_status = ? WHERE id = ?',
            (status, signal_id),
        )


# ---------------------------------------------------------------------------
# signal_updates (follow-ups: price updates, trims, closes, stops, notes)
# ---------------------------------------------------------------------------

def insert_signal_update(signal_id, message_id, action, source, parser_version,
                         fraction=None, contracts=None, price=None, pct_gain=None,
                         stop_price=None, stop_pct=None, confidence=None,
                         attach_rule=None, raw_excerpt=None):
    with get_connection() as conn:
        cursor = conn.execute('''
            INSERT OR REPLACE INTO signal_updates
                (signal_id, message_id, action, fraction, contracts, price,
                 pct_gain, stop_price, stop_pct, confidence, source, attach_rule,
                 raw_excerpt, parser_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (signal_id, message_id, action, fraction, contracts, price,
              pct_gain, stop_price, stop_pct, confidence, source, attach_rule,
              raw_excerpt, parser_version))
        return cursor.lastrowid


def get_signal_updates(signal_id):
    with get_connection() as conn:
        rows = conn.execute('''
            SELECT u.*, m.created_at AS message_created_at, m.content
            FROM signal_updates u
            JOIN messages m ON m.id = u.message_id
            WHERE u.signal_id = ?
            ORDER BY m.created_at ASC, u.id ASC
        ''', (signal_id,)).fetchall()
        return [dict(row) for row in rows]


def get_update_by_message_id(message_id):
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM signal_updates WHERE message_id = ?', (message_id,),
        ).fetchone()
        return dict(row) if row else None


def get_last_activity(author_id, before=None):
    """Most recent entry or update by this author, with its signal_id and time.

    Used by the 'standalone message shortly after the author's last update'
    attachment rule.
    """
    before = before or datetime.utcnow().isoformat()
    with get_connection() as conn:
        row = conn.execute('''
            SELECT signal_id, at FROM (
                SELECT s.id AS signal_id, m.created_at AS at
                FROM option_signals s JOIN messages m ON m.id = s.message_id
                WHERE m.author_id = ? AND m.created_at < ?
                UNION ALL
                SELECT u.signal_id, m.created_at
                FROM signal_updates u JOIN messages m ON m.id = u.message_id
                WHERE m.author_id = ? AND m.created_at < ?
            )
            ORDER BY at DESC LIMIT 1
        ''', (str(author_id), before, str(author_id), before)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# needs_label
# ---------------------------------------------------------------------------

def insert_needs_label(message_id, reason, candidate=None):
    with get_connection() as conn:
        conn.execute('''
            INSERT OR REPLACE INTO needs_label (message_id, reason, candidate)
            VALUES (?, ?, ?)
        ''', (message_id, reason, candidate))


def get_needs_label(include_resolved=False):
    with get_connection() as conn:
        sql = '''
            SELECT n.*, m.author_name, m.content, m.created_at AS message_created_at,
                   m.reply_to_id
            FROM needs_label n JOIN messages m ON m.id = n.message_id
        '''
        if not include_resolved:
            sql += ' WHERE n.resolved_at IS NULL'
        sql += ' ORDER BY m.created_at ASC'
        return [dict(row) for row in conn.execute(sql).fetchall()]


def resolve_needs_label(message_id):
    with get_connection() as conn:
        conn.execute('''
            UPDATE needs_label SET resolved_at = datetime('now')
            WHERE message_id = ? AND resolved_at IS NULL
        ''', (message_id,))


# ---------------------------------------------------------------------------
# paper_trades (Phase 3 will replace this with `orders`)
# ---------------------------------------------------------------------------

def insert_trade(signal_id, side, quantity, broker_order_id=None,
                 fill_price=None, status='pending', error_message=None,
                 submitted_at=None, filled_at=None):
    with get_connection() as conn:
        cursor = conn.execute('''
            INSERT INTO paper_trades
                (signal_id, broker_order_id, side, quantity, fill_price,
                 status, error_message, submitted_at, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (signal_id, broker_order_id, side, quantity, fill_price,
              status, error_message, submitted_at, filled_at))
        return cursor.lastrowid


def update_trade(trade_id, status, broker_order_id=None, fill_price=None,
                 error_message=None, submitted_at=None, filled_at=None):
    with get_connection() as conn:
        conn.execute('''
            UPDATE paper_trades
            SET status = ?,
                broker_order_id = COALESCE(?, broker_order_id),
                fill_price = COALESCE(?, fill_price),
                error_message = COALESCE(?, error_message),
                submitted_at = COALESCE(?, submitted_at),
                filled_at = COALESCE(?, filled_at)
            WHERE id = ?
        ''', (status, broker_order_id, fill_price, error_message,
              submitted_at, filled_at, trade_id))


# ---------------------------------------------------------------------------
# positions / orders (Phase 3)
# ---------------------------------------------------------------------------

def get_signals_awaiting_entry():
    with get_connection() as conn:
        rows = conn.execute('''
            SELECT s.*, m.created_at AS message_created_at
            FROM option_signals s
            JOIN messages m ON m.id = s.message_id
            LEFT JOIN positions p ON p.signal_id = s.id
            WHERE s.trade_status = 'pending' AND s.action = 'buy' AND p.id IS NULL
            ORDER BY m.created_at ASC
        ''').fetchall()
        return [dict(r) for r in rows]


def get_pending_updates():
    with get_connection() as conn:
        rows = conn.execute('''
            SELECT u.*, m.created_at AS message_created_at
            FROM signal_updates u
            JOIN messages m ON m.id = u.message_id
            WHERE u.execution_status = 'pending'
            ORDER BY m.created_at ASC, u.id ASC
        ''').fetchall()
        return [dict(r) for r in rows]


def mark_update_executed(update_id, status, note=None):
    with get_connection() as conn:
        conn.execute('''
            UPDATE signal_updates
            SET execution_status = ?, execution_note = ?, executed_at = datetime('now')
            WHERE id = ?
        ''', (status, note, update_id))


def insert_position(signal_id, broker, contract_symbol, qty_target, source_entry=None,
                    entry_bid=None, entry_ask=None, entry_delta=None, entry_iv=None,
                    status='opening'):
    with get_connection() as conn:
        cur = conn.execute('''
            INSERT INTO positions
                (signal_id, broker, contract_symbol, qty_target, source_entry,
                 entry_bid, entry_ask, entry_delta, entry_iv, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (signal_id, broker, contract_symbol, qty_target, source_entry,
              entry_bid, entry_ask, entry_delta, entry_iv, status))
        return cur.lastrowid


_POSITION_SELECT = '''
    SELECT p.*, s.ticker, s.option_type, s.strike, s.expiration
    FROM positions p JOIN option_signals s ON s.id = p.signal_id
'''


def get_position(position_id):
    with get_connection() as conn:
        row = conn.execute(_POSITION_SELECT + ' WHERE p.id = ?', (position_id,)).fetchone()
        return dict(row) if row else None


def get_position_by_signal(signal_id):
    with get_connection() as conn:
        row = conn.execute(_POSITION_SELECT + ' WHERE p.signal_id = ?', (signal_id,)).fetchone()
        return dict(row) if row else None


def get_positions(statuses=('opening', 'open')):
    with get_connection() as conn:
        marks = ','.join('?' * len(statuses))
        rows = conn.execute(
            _POSITION_SELECT + f' WHERE p.status IN ({marks}) ORDER BY p.id',
            tuple(statuses)).fetchall()
        return [dict(r) for r in rows]


def update_position(position_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with get_connection() as conn:
        conn.execute(f'UPDATE positions SET {cols} WHERE id = ?',
                     (*fields.values(), position_id))


def insert_order(position_id, broker, side, reason, qty, limit_price, update_id=None,
                 broker_order_id=None, client_order_id=None, status='new',
                 submitted_at=None, error=None):
    with get_connection() as conn:
        cur = conn.execute('''
            INSERT INTO orders
                (position_id, update_id, broker, broker_order_id, client_order_id,
                 side, reason, qty, limit_price, status, submitted_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (position_id, update_id, broker, broker_order_id, client_order_id,
              side, reason, qty, limit_price, status, submitted_at, error))
        return cur.lastrowid


def update_order(order_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    with get_connection() as conn:
        conn.execute(f'UPDATE orders SET {cols} WHERE id = ?',
                     (*fields.values(), order_id))


def get_order(order_id):
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
        return dict(row) if row else None


def get_working_orders(position_id=None, side=None):
    with get_connection() as conn:
        sql = '''
            SELECT * FROM orders
            WHERE status NOT IN ('filled','canceled','cancelled','expired','rejected','error')
              AND broker_order_id IS NOT NULL
        '''
        params = []
        if position_id is not None:
            sql += ' AND position_id = ?'; params.append(position_id)
        if side is not None:
            sql += ' AND side = ?'; params.append(side)
        sql += ' ORDER BY id'
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_realized_pnl_since(since_iso):
    with get_connection() as conn:
        row = conn.execute('''
            SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM positions
            WHERE COALESCE(closed_at, created_at) >= ? OR status IN ('open','opening')
        ''', (since_iso,)).fetchone()
        return float(row['pnl'])


# ---------------------------------------------------------------------------
# replay support
# ---------------------------------------------------------------------------

def clear_parse_results():
    """Wipe everything derived from messages so the parser can be re-run."""
    with get_connection() as conn:
        conn.execute('DELETE FROM needs_label')
        conn.execute('DELETE FROM orders')
        conn.execute('DELETE FROM positions')
        conn.execute('DELETE FROM signal_updates')
        conn.execute('DELETE FROM paper_trades')
        conn.execute('DELETE FROM option_signals')
        conn.execute("UPDATE messages SET parse_status = 'pending'")


if __name__ == '__main__':
    init_db()
    print(get_messages())
