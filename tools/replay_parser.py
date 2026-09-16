"""Re-run the parser over every stored message.

    python -m tools.replay_parser            # dry run: print what would change
    python -m tools.replay_parser --apply    # wipe derived rows and re-parse
    python -m tools.replay_parser --apply --no-llm

Messages are processed in created_at order so the attachment rules see the
same history they would have seen live.
"""
import argparse
import sqlite3
from collections import Counter

from database import (
    DB_PATH,
    clear_parse_results,
    get_all_messages,
    get_connection,
    init_db,
)
from pipeline import process_message


def _snapshot_status():
    with get_connection() as conn:
        return {
            r['id']: r['parse_status']
            for r in conn.execute('SELECT id, parse_status FROM messages')
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='actually rewrite option_signals / signal_updates / needs_label')
    ap.add_argument('--no-llm', action='store_true', help='skip the LLM fallback')
    ap.add_argument('--verbose', '-v', action='store_true')
    args = ap.parse_args()

    init_db()
    before = _snapshot_status()

    if not args.apply:
        # Dry run: work on a throwaway in-memory copy of the DB. Every helper
        # in database.py resolves get_connection() from module globals at
        # call time, so swapping that one name redirects the whole run.
        import database
        src = sqlite3.connect(DB_PATH)
        tmp = sqlite3.connect(':memory:')
        src.backup(tmp)
        src.close()
        database.get_connection = _memory_connection(tmp)

    clear_parse_results()
    outcomes = Counter()
    kinds = Counter()
    for msg in get_all_messages():
        result = process_message(msg, use_llm=not args.no_llm)
        outcomes[result['status']] += 1
        if result['status'] == 'parsed':
            kinds[result.get('kind')] += 1
        if args.verbose:
            snippet = (msg['content'] or '').replace('\n', ' / ')[:70]
            print(f"[{msg['id']:>4}] {result['status']:<11} {result.get('kind', result.get('reason', '')):<14} {snippet!r}")

    after = _snapshot_status()
    changed = [(mid, before.get(mid), after.get(mid))
               for mid in after if before.get(mid) != after.get(mid)]

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {sum(outcomes.values())} messages")
    print('outcomes:', dict(outcomes))
    print('parsed kinds:', dict(kinds))
    print(f'status changes vs previous parse: {len(changed)}')
    for mid, old, new in changed[:40]:
        print(f'  message {mid}: {old} -> {new}')


def _memory_connection(conn):
    from contextlib import contextmanager
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')

    @contextmanager
    def get_connection():
        try:
            yield conn
            conn.commit()
        finally:
            pass
    return get_connection


if __name__ == '__main__':
    main()
