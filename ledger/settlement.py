from collections.abc import Callable
from pathlib import Path
from sqlite3 import Connection

from ledger.claims import verify_claims
from ledger.recovery import recover_custody
from ledger.store import CustodyRecord, UnconfirmedCustody, custody_transaction, load_custody_record
from review.candidates import Assignment


def settle_assignment(path: Path, request_id: str, batch_id: str, parameters: str,
                      assignment: Assignment, report_id: str,
                      before_commit: Callable[[], None] | None = None) -> tuple[str, bool]:
    """Record the terminal disposition of already prepared, immutable report material."""
    with custody_transaction(path, write=True) as db:
        record = recover_custody(db, request_id, batch_id, parameters)
        if record is None:
            raise UnconfirmedCustody('custody record is unavailable')
        if record.state == 'cancelled':
            return 'cancelled', False
        if record.assignment != assignment:
            raise UnconfirmedCustody('prepared report differs from the reserved assignment')
        if record.state == 'issued':
            if record.report_id != report_id:
                raise UnconfirmedCustody('a different report owns this assignment')
            return 'issued', False
        db.execute('UPDATE custody_requests SET state = ?, report_id = ? WHERE request_id = ?',
                   ('issued', report_id, request_id))
        if before_commit is not None:
            before_commit()
        return 'issued', True


def load_settlement(db: Connection, request_id: str) -> CustodyRecord | None:
    record = load_custody_record(db, request_id)
    if record is None or record.state != 'issued':
        return None
    verify_claims(db, record)
    return record
