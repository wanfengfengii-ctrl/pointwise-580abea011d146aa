"""Append-only custody event log and the shift registry backing shift statements.

Events are recorded inside the same ledger transaction that commits the custody
change, so their sequence numbers follow the actual commit order. Shift rows fix
the opening boundary at registration and the closing boundary at close time.
"""
import json
from collections.abc import Iterable
from dataclasses import dataclass
from sqlite3 import Connection


@dataclass(frozen=True)
class CustodyEvent:
    seq: int
    kind: str
    request_id: str
    batch_id: str
    report_id: str | None
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ShiftRecord:
    shift_id: str
    batch_id: str
    opened_seq: int
    opening_balance: dict
    evidence: dict
    closed_seq: int | None
    closing_balance: dict | None

    @property
    def closed(self) -> bool:
        return self.closed_seq is not None


def record_event(db: Connection, kind: str, request_id: str, batch_id: str,
                 report_id: str | None, assignment: Iterable[tuple[str, str]]) -> None:
    lines = json.dumps([[slot, key] for slot, key in assignment], separators=(',', ':'))
    db.execute('''INSERT INTO custody_events (request_id, batch_id, kind, report_id, lines)
                  VALUES (?, ?, ?, ?, ?)''', (request_id, batch_id, kind, report_id, lines))


def current_event_seq(db: Connection) -> int:
    row = db.execute('SELECT COALESCE(MAX(seq), 0) AS seq FROM custody_events').fetchone()
    return row['seq']


def list_events(db: Connection, after_seq: int, through_seq: int) -> list[CustodyEvent]:
    rows = db.execute('''SELECT seq, kind, request_id, batch_id, report_id, lines
                         FROM custody_events WHERE seq > ? AND seq <= ? ORDER BY seq''',
                      (after_seq, through_seq))
    return [CustodyEvent(row['seq'], row['kind'], row['request_id'], row['batch_id'],
                         row['report_id'],
                         tuple(tuple(pair) for pair in json.loads(row['lines'])))
            for row in rows]


def load_shift(db: Connection, shift_id: str) -> ShiftRecord | None:
    row = db.execute('SELECT * FROM shifts WHERE shift_id = ?', (shift_id,)).fetchone()
    if row is None:
        return None
    closing = row['closing_balance']
    return ShiftRecord(row['shift_id'], row['batch_id'], row['opened_seq'],
                       json.loads(row['opening_balance']), json.loads(row['evidence']),
                       row['closed_seq'], None if closing is None else json.loads(closing))


def insert_shift(db: Connection, shift_id: str, batch_id: str, opened_seq: int,
                 opening_balance: dict, evidence: dict) -> None:
    db.execute('''INSERT INTO shifts (shift_id, batch_id, opened_seq, opening_balance, evidence)
                  VALUES (?, ?, ?, ?, ?)''',
               (shift_id, batch_id, opened_seq, json.dumps(opening_balance, separators=(',', ':')),
                json.dumps(evidence, separators=(',', ':'))))


def mark_shift_closed(db: Connection, shift_id: str, closed_seq: int,
                      closing_balance: dict) -> None:
    db.execute('UPDATE shifts SET closed_seq = ?, closing_balance = ? WHERE shift_id = ?',
               (closed_seq, json.dumps(closing_balance, separators=(',', ':')), shift_id))
