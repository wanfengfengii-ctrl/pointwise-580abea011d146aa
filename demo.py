import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.initialize import initialize
from archive.publish import AFTER_COMMIT, AFTER_PREPARE, ConfirmationInterrupted, sign
from archive.readback import read
from archive.store import Stores
from audit.shift import audit, close_shift, open_shift, read_shift, seal_shift
from ledger.cancel import cancel
from review.candidates import RequestSlot
from review.criteria import Gauge, SlotRequirement
from review.freeze import freeze
from shift_cases import load_case
from source.ingest import ingest
from source.origins import Origin
from tests.support import stop_at


def assignments(report):
    return [[line['slot'], line['measurement']['ref']['measurement']] for line in report['lines']]


def shift_demo() -> None:
    with TemporaryDirectory() as directory:
        stores = initialize(directory)
        refs, x, y = load_case(stores)
        opened = open_shift(stores, 'day', 'S')
        sign(stores, 'neighbor', 'X', x, checkpoint=stop_at(AFTER_PREPARE))
        open_shift(stores, 'day', 'T')
        cancel(stores, 'neighbor', 'X', x)
        sign(stores, 'day', 'Y', y, checkpoint=stop_at(AFTER_PREPARE))
        broken = Stores(Path(directory) / 'archive-down' / 'archive.sqlite3', stores.ledger)
        closed_s = close_shift(broken, 'S')
        with sqlite3.connect(stores.archive, isolation_level=None) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            try:
                blocked_seal = seal_shift(stores, 'S')
            finally:
                blocker.rollback()
        not_sealed = read_shift(stores, 'S')
        sign(stores, 'day', 'Y', y)
        closed_t = close_shift(stores, 'T')
        reclosed_s = close_shift(stores, 'S')
        sealed_s = seal_shift(stores, 'S')
        sealed_t = seal_shift(stores, 'T')
        reopened = initialize(directory)
        collected_s = read_shift(reopened, 'S')
        open_shift(reopened, 'day', 'E')
        close_shift(reopened, 'E')
        empty = seal_shift(reopened, 'E')
        expected_kinds = {'S': ['hold', 'release', 'hold'], 'T': ['release', 'hold', 'issue']}
        actual_kinds = {tag: [event['kind'] for event in statement['events']]
                        for tag, statement in (('S', sealed_s), ('T', sealed_t))}
        downstream = [closed_s['status'], blocked_seal['status'], not_sealed['status'],
                      sealed_s['closing_balance']['held'], collected_s == sealed_s,
                      empty['events']]
        downstream_expected = ['closed', 'pending', 'not_sealed', 1, True, []]
        checks = [
            opened == {'status': 'open', 'batch_id': 'day', 'shift_id': 'S'},
            closed_s == {'status': 'closed', 'batch_id': 'day', 'shift_id': 'S'},
            reclosed_s == closed_s,
            closed_t == {'status': 'closed', 'batch_id': 'day', 'shift_id': 'T'},
            actual_kinds == expected_kinds,
            downstream == downstream_expected,
            sealed_s['receipt'] == {'receipt_id': 'receipt:shift:S', 'shift_id': 'S',
                                    'batch_id': 'day'},
            read_shift(reopened, 'T') == sealed_t,
        ]
        if not all(checks):
            raise SystemExit(f'shift handover demo differed from expected results: {checks}')
        scenario = {'case_id': 'shift.handover',
                    'input': {'close_S': 'archive path unopenable',
                              'first_seal_S': 'archive write blocked, then late issue of Y',
                              'reopen': 'collect S and T by shift id'},
                    'expected': expected_kinds, 'actual': actual_kinds,
                    'downstream_expected': downstream_expected, 'downstream_actual': downstream,
                    'code_paths': ['ledger/shiftbook.py', 'ledger/claims.py', 'ledger/settlement.py',
                                   'ledger/cancel.py', 'audit/shift.py', 'archive/store.py'],
                    'oracle': 'S closes on the ledger alone while the archive is down; the blocked '
                              'first seal stores nothing; the late issue stays out of S, and '
                              'reopening collects the same statement and receipt.'}
        print('POINTWISE_SCENARIO ' + json.dumps(scenario, ensure_ascii=False, sort_keys=True))
        print(json.dumps({'closed_without_archive': closed_s, 'blocked_first_seal': blocked_seal,
                          'after_failed_seal': not_sealed, 'reclosed_after_late_issue': reclosed_s,
                          'sealed_S': sealed_s, 'sealed_T': sealed_t,
                          'collected_S_after_reopen': collected_s, 'empty_shift': empty},
                         ensure_ascii=False, indent=2))


def main() -> None:
    sample = json.loads((Path(__file__).resolve().parent / 'samples' / 'day.json').read_text(encoding='utf-8'))
    with TemporaryDirectory() as directory:
        stores = initialize(directory)
        refs = {}
        for item in sample['readings']:
            refs[item['id']] = ingest(
                stores, sample['device'], item['id'], unit='um', reading=item['reading_um'],
                category=item['category'], origin=Origin(sample['probe_batch'], sample['document'], item['line']),
            )
        slots = tuple(SlotRequirement(item['name'], tuple(item['categories']),
                                      tuple(refs[name] for name in item['candidates'])) for item in sample['slots'])
        freeze(stores, 'day', refs.values(), Gauge(*sample['gauge_um']), slots)
        x = tuple(RequestSlot(slot, tuple(refs[name] for name in names)) for slot, names in sample['request_x'])
        y = tuple(RequestSlot(slot, tuple(refs[name] for name in names)) for slot, names in sample['request_y'])
        observed = []

        def lose_reply(stage):
            if stage == AFTER_COMMIT:
                observed.append(read(stores, 'R-X'))
                raise ConfirmationInterrupted(stage)

        interrupted = sign(stores, 'day', 'X', x, checkpoint=lose_reply)
        stores = initialize(directory)
        aborted = cancel(stores, 'day', 'X', x)
        competitor = sign(stores, 'day', 'Y', y)
        correction = sample['correction']
        ingest(stores, sample['device'], correction['id'], unit='um', reading=correction['reading_um'],
               category=correction['category'],
               origin=Origin(sample['probe_batch'], correction['document'], correction['line']),
               previous=refs[correction['id']])
        stores = initialize(directory)
        report = read(stores, 'R-X')
        statement = audit(stores, 'day', 'R-X')
        statuses = [interrupted['status'], aborted['status'], competitor['status'], report['status']]
        if statuses != ['pending', 'already_signed', 'not_enough', 'signed'] or observed != [report]:
            raise SystemExit(f'daily issue and recovery differed from expected results: {statuses}')
        expected = [['U', 'B'], ['V', 'A']]
        actual = assignments(report)
        balance = {key: statement['balance'][key] for key in ('used', 'available', 'missing')}
        downstream_expected = {'balance': {'used': 2, 'available': 0, 'missing': 1}, 'matches': True,
                               'receipt': 'receipt:X', 'B_document': 'probe-day.csv'}
        downstream_actual = {'balance': balance, 'matches': statement['report']['all_match'],
                             'receipt': report['receipt']['receipt_id'],
                             'B_document': report['lines'][0]['measurement']['origin']['document']}
        if actual != expected or downstream_actual != downstream_expected:
            raise SystemExit(f'daily issuance or shift reconciliation differed: {actual}, {downstream_actual}')
        scenario = {'case_id': 'match', 'input': sample, 'expected': expected, 'actual': actual,
                    'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
                    'code_paths': ['review/matching.py', 'archive/publish.py', 'archive/readback.py', 'audit/shift.py'],
                    'oracle': 'V requires A, B is the remaining U candidate, and M cannot be spent. The old report keeps B line 2.'}
        print('POINTWISE_SCENARIO ' + json.dumps(scenario, ensure_ascii=False, sort_keys=True))
        print(json.dumps({'report': report, 'balance': statement['balance'], 'competing_request': competitor},
                         ensure_ascii=False, indent=2))
    shift_demo()


if __name__ == '__main__':
    main()
