from sqlite3 import Connection, OperationalError

from archive.store import (Stores, archive_transaction, binding_for_report, list_batch_bindings,
                           load_shift_statement, save_shift_statement)
from audit.balance import BALANCE_STATES, count_balance
from audit.by_evidence import reconcile_evidence
from audit.by_report import reconcile_report
from ledger.recovery import list_unconfirmed_requests
from ledger.shiftbook import (CustodyEvent, ShiftRecord, current_event_seq, insert_shift,
                              list_events, load_shift, mark_shift_closed)
from ledger.store import (UnconfirmedCustody, custody_transaction, load_evidence_custody,
                          pin_custody_snapshot)
from review.batches import FrozenBatch, load_frozen_batch
from source.origins import require_label


def _statement(archive_db: Connection, ledger_db: Connection | None, batch_id: str,
               report_id: str | None) -> dict:
    batch = load_frozen_batch(archive_db, batch_id)
    if batch is None:
        return {'status': 'unknown_batch'}
    report = None
    if report_id is not None:
        binding = binding_for_report(archive_db, report_id)
        if binding is not None and binding.batch_id != batch_id:
            return {'status': 'batch_mismatch'}
        report = reconcile_report(archive_db, ledger_db, batch, report_id)
    evidence = reconcile_evidence(ledger_db, batch)
    if ledger_db is None:
        unconfirmed = list_batch_bindings(archive_db, batch_id)
    else:
        unconfirmed = list_unconfirmed_requests(ledger_db, batch_id)
        if report is not None and report['status'] == 'pending' and binding is not None:
            unconfirmed = sorted({*unconfirmed, binding.request_id})
    return {'status': 'pending' if ledger_db is None else 'ok', 'batch_id': batch_id,
            'balance': count_balance(evidence), 'evidence': evidence,
            'pending_requests': unconfirmed, 'report': report}


def audit(stores: Stores, batch_id: str, report_id: str | None = None) -> dict:
    require_label(batch_id, 'batch id')
    if report_id is not None:
        require_label(report_id, 'report id')
    try:
        with custody_transaction(stores.ledger) as ledger_db:
            # The ledger snapshot can only issue archive material that already committed.
            pin_custody_snapshot(ledger_db)
            with archive_transaction(stores.archive) as archive_db:
                return _statement(archive_db, ledger_db, batch_id, report_id)
    except (OperationalError, UnconfirmedCustody):
        try:
            with archive_transaction(stores.archive) as archive_db:
                return _statement(archive_db, None, batch_id, report_id)
        except OperationalError:
            return {'status': 'pending', 'batch_id': batch_id}


def _static_classes(batch: FrozenBatch) -> dict[str, str]:
    """Per-evidence classification that does not depend on custody: missing,
    qualified (eligible for at least one slot) or ineligible."""
    qualified = {option.ref.key for options in batch.qualifications
                 for option in options if option.exclusion is None}
    classes = {}
    for revision in batch.measurements:
        key = revision.ref.key
        if revision.reading_um is None:
            classes[key] = 'missing'
        elif key in qualified:
            classes[key] = 'qualified'
        else:
            classes[key] = 'ineligible'
    return classes


def _balance_for(classes: dict[str, str], claims: dict) -> dict[str, int]:
    """Six-category balance for the batch evidence, following the audit rules."""
    counts = dict.fromkeys(BALANCE_STATES, 0)
    for key, status in classes.items():
        claim = claims.get(key)
        if status == 'missing':
            counts['missing'] += 1
        elif claim is not None:
            if claim.state == 'cancelled':
                raise UnconfirmedCustody('cancelled request has an active claim')
            counts['used' if claim.state == 'issued' else 'held'] += 1
        else:
            counts['available' if status == 'qualified' else 'ineligible'] += 1
    return counts


def open_shift(stores: Stores, batch_id: str, shift_id: str) -> dict:
    require_label(batch_id, 'batch id')
    require_label(shift_id, 'shift id')
    try:
        with archive_transaction(stores.archive) as archive_db:
            batch = load_frozen_batch(archive_db, batch_id)
        if batch is None:
            return {'status': 'unknown_batch'}
        classes = _static_classes(batch)
        with custody_transaction(stores.ledger, write=True) as ledger_db:
            shift = load_shift(ledger_db, shift_id)
            if shift is not None:
                if shift.batch_id != batch_id:
                    return {'status': 'shift_conflict'}
                return {'status': 'closed' if shift.closed else 'open',
                        'batch_id': batch_id, 'shift_id': shift_id}
            opening = _balance_for(classes, load_evidence_custody(ledger_db, classes))
            insert_shift(ledger_db, shift_id, batch_id, current_event_seq(ledger_db),
                         opening, classes)
            return {'status': 'open', 'batch_id': batch_id, 'shift_id': shift_id}
    except (OperationalError, UnconfirmedCustody):
        return {'status': 'pending', 'shift_id': shift_id}


def close_shift(stores: Stores, shift_id: str) -> dict:
    require_label(shift_id, 'shift id')
    try:
        with custody_transaction(stores.ledger, write=True) as ledger_db:
            shift = load_shift(ledger_db, shift_id)
            if shift is None:
                return {'status': 'unknown_shift'}
            if shift.closed:
                return {'status': 'closed', 'batch_id': shift.batch_id, 'shift_id': shift_id}
            closing = _balance_for(shift.evidence,
                                   load_evidence_custody(ledger_db, shift.evidence))
            mark_shift_closed(ledger_db, shift_id, current_event_seq(ledger_db), closing)
            return {'status': 'closed', 'batch_id': shift.batch_id, 'shift_id': shift_id}
    except (OperationalError, UnconfirmedCustody):
        return {'status': 'pending', 'shift_id': shift_id}


def _sealed_statement(shift: ShiftRecord, events: list[CustodyEvent]) -> dict:
    keys = set(shift.evidence)
    entries = []
    for event in events:
        lines = [{'slot': slot, 'evidence_id': key} for slot, key in event.lines if key in keys]
        if lines:
            entries.append({'kind': event.kind, 'request_id': event.request_id,
                            'report_id': event.report_id, 'lines': lines})
    return {'status': 'sealed', 'shift_id': shift.shift_id, 'batch_id': shift.batch_id,
            'opening_balance': shift.opening_balance, 'closing_balance': shift.closing_balance,
            'events': entries,
            'receipt': {'receipt_id': f'receipt:shift:{shift.shift_id}',
                        'shift_id': shift.shift_id, 'batch_id': shift.batch_id}}


def seal_shift(stores: Stores, shift_id: str) -> dict:
    require_label(shift_id, 'shift id')
    try:
        with custody_transaction(stores.ledger) as ledger_db:
            shift = load_shift(ledger_db, shift_id)
            if shift is None:
                return {'status': 'unknown_shift'}
            if not shift.closed:
                return {'status': 'not_closed'}
            events = list_events(ledger_db, shift.opened_seq, shift.closed_seq)
        statement = _sealed_statement(shift, events)
        with archive_transaction(stores.archive, write=True) as archive_db:
            existing = load_shift_statement(archive_db, shift_id)
            if existing is not None:
                if existing.get('batch_id') != shift.batch_id:
                    raise UnconfirmedCustody('stored shift statement names a different batch')
                return existing
            save_shift_statement(archive_db, shift_id, shift.batch_id, statement)
            return load_shift_statement(archive_db, shift_id)
    except (OperationalError, UnconfirmedCustody):
        return {'status': 'pending', 'shift_id': shift_id}


def read_shift(stores: Stores, shift_id: str) -> dict:
    require_label(shift_id, 'shift id')
    try:
        with custody_transaction(stores.ledger) as ledger_db:
            shift = load_shift(ledger_db, shift_id)
        if shift is None:
            return {'status': 'unknown_shift'}
        with archive_transaction(stores.archive) as archive_db:
            statement = load_shift_statement(archive_db, shift_id)
        if statement is None:
            return {'status': 'not_sealed', 'shift_id': shift_id}
        return statement
    except OperationalError:
        return {'status': 'pending', 'shift_id': shift_id}
