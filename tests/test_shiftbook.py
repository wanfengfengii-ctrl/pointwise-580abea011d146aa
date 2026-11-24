import json
import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.initialize import initialize
from archive.publish import AFTER_PREPARE, BEFORE_COMMIT, sign
from archive.store import Stores, archive_transaction
from audit.shift import audit, close_shift, open_shift, read_shift, seal_shift
from ledger.cancel import cancel
from ledger.store import custody_transaction
from review.candidates import RequestSlot
from shift_cases import load_case
from tests.support import stop_at

ZERO = {'used': 0, 'available': 0, 'held': 0, 'missing': 0, 'ineligible': 0, 'unconfirmed': 0}


class ShiftBookTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.directory = Path(directory)
        self.stores = initialize(self.directory)
        self.refs, self.x, self.y = load_case(self.stores)
        self.keys = {name: ref.key for name, ref in self.refs.items()}

    def scenario(self, case_id, expected, actual, oracle):
        self.assertEqual(actual, expected)
        row = dict(case_id=case_id, expected=expected, actual=actual, oracle=oracle)
        print('POINTWISE_SCENARIO ' + json.dumps(row, ensure_ascii=False, sort_keys=True))

    @contextmanager
    def blocked_ledger(self):
        with closing(sqlite3.connect(self.stores.ledger, isolation_level=None)) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            try:
                yield
            finally:
                blocker.rollback()

    @contextmanager
    def blocked_archive(self):
        with closing(sqlite3.connect(self.stores.archive, isolation_level=None)) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            try:
                yield
            finally:
                blocker.rollback()

    def broken_archive_stores(self):
        return Stores(self.directory / 'archive-down' / 'archive.sqlite3', self.stores.ledger)

    def prepare_x(self):
        return sign(self.stores, 'neighbor', 'X', self.x, checkpoint=stop_at(AFTER_PREPARE))

    def prepare_y(self):
        return sign(self.stores, 'day', 'Y', self.y, checkpoint=stop_at(AFTER_PREPARE))

    def x_lines(self):
        return [{'slot': 'N2', 'evidence_id': self.keys['D']},
                {'slot': 'N1', 'evidence_id': self.keys['A']}]

    def y_lines(self):
        return [{'slot': 'U', 'evidence_id': self.keys['B']}]

    def test_fixture_statements_follow_commit_order(self):
        self.assertEqual(open_shift(self.stores, 'day', 'S'),
                         {'status': 'open', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(self.prepare_x()['status'], 'pending')
        self.assertEqual(open_shift(self.stores, 'day', 'T'),
                         {'status': 'open', 'batch_id': 'day', 'shift_id': 'T'})
        self.assertEqual(cancel(self.stores, 'neighbor', 'X', self.x)['status'], 'cancelled')
        self.assertEqual(self.prepare_y()['status'], 'pending')
        self.assertEqual(close_shift(self.stores, 'S'),
                         {'status': 'closed', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y,
                              checkpoint=stop_at(BEFORE_COMMIT))['status'], 'pending')
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y)['status'], 'signed')
        self.assertEqual(close_shift(self.stores, 'T'),
                         {'status': 'closed', 'batch_id': 'day', 'shift_id': 'T'})
        s, t = seal_shift(self.stores, 'S'), seal_shift(self.stores, 'T')
        self.scenario('shiftbook.fixture.balances',
                      {'S_open': {**ZERO, 'available': 2, 'missing': 1, 'ineligible': 1},
                       'S_close': {**ZERO, 'available': 1, 'held': 1, 'missing': 1, 'ineligible': 1},
                       'T_open': {**ZERO, 'available': 1, 'held': 2, 'missing': 1},
                       'T_close': {**ZERO, 'used': 1, 'available': 1, 'missing': 1, 'ineligible': 1}},
                      {'S_open': s['opening_balance'], 'S_close': s['closing_balance'],
                       'T_open': t['opening_balance'], 'T_close': t['closing_balance']},
                      'Hand-computed boundary balances for the overlapping shifts S and T.')
        self.scenario('shiftbook.fixture.events',
                      {'S': [['hold', 'X'], ['release', 'X'], ['hold', 'Y']],
                       'T': [['release', 'X'], ['hold', 'Y'], ['issue', 'Y']]},
                      {'S': [[e['kind'], e['request_id']] for e in s['events']],
                       'T': [[e['kind'], e['request_id']] for e in t['events']]},
                      'Events keep ledger commit order; the rolled-back issue left no entry.')
        self.assertEqual(s['events'][0]['lines'], self.x_lines())
        self.assertEqual(s['events'][1]['lines'], self.x_lines())
        self.assertEqual(s['events'][2]['lines'], self.y_lines())
        self.assertIsNone(s['events'][0]['report_id'])
        self.assertIsNone(s['events'][1]['report_id'])
        self.assertEqual(t['events'][2]['report_id'], 'R-Y')
        self.assertEqual(t['events'][2]['lines'], self.y_lines())
        self.assertEqual(s['receipt'],
                         {'receipt_id': 'receipt:shift:S', 'shift_id': 'S', 'batch_id': 'day'})
        self.assertEqual(read_shift(self.stores, 'S'), s)
        self.assertEqual(seal_shift(self.stores, 'S'), s)
        self.assertEqual(audit(self.stores, 'day')['balance'], t['closing_balance'])

    def test_event_order_swaps_with_commit_order(self):
        open_shift(self.stores, 'day', 'S')
        self.prepare_x()
        open_shift(self.stores, 'day', 'T')
        self.assertEqual(self.prepare_y()['status'], 'pending')
        self.assertEqual(cancel(self.stores, 'neighbor', 'X', self.x)['status'], 'cancelled')
        close_shift(self.stores, 'S')
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y)['status'], 'signed')
        close_shift(self.stores, 'T')
        s, t = seal_shift(self.stores, 'S'), seal_shift(self.stores, 'T')
        self.scenario('shiftbook.fixture.reversed',
                      {'S': [['hold', 'X'], ['hold', 'Y'], ['release', 'X']],
                       'T': [['hold', 'Y'], ['release', 'X'], ['issue', 'Y']]},
                      {'S': [[e['kind'], e['request_id']] for e in s['events']],
                       'T': [[e['kind'], e['request_id']] for e in t['events']]},
                      'Preparing Y before cancelling X swaps the two entries in both statements.')

    def test_close_without_archive_and_seal_after_recovery(self):
        open_shift(self.stores, 'day', 'S')
        self.assertEqual(self.prepare_y()['status'], 'pending')
        broken = self.broken_archive_stores()
        closed = close_shift(broken, 'S')
        with self.blocked_archive():
            blocked = seal_shift(self.stores, 'S')
        not_sealed = read_shift(self.stores, 'S')
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y)['status'], 'signed')
        reclosed = close_shift(self.stores, 'S')
        sealed = seal_shift(self.stores, 'S')
        reopened = initialize(self.directory)
        self.scenario('shiftbook.archive_down',
                      {'close': 'closed', 'blocked_seal': 'pending', 'after_unblock': 'not_sealed',
                       'reclose': 'closed', 'events': [['hold', 'Y']], 'closing_held': 1},
                      {'close': closed['status'], 'blocked_seal': blocked['status'],
                       'after_unblock': not_sealed['status'], 'reclose': reclosed['status'],
                       'events': [[e['kind'], e['request_id']] for e in sealed['events']],
                       'closing_held': sealed['closing_balance']['held']},
                      'S closed on the ledger alone; the blocked first seal stored nothing; '
                      'the late issue stays out of the fixed closing boundary.')
        self.assertEqual(closed, {'status': 'closed', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(reclosed, closed)
        self.assertEqual(sealed['events'][0]['lines'], self.y_lines())
        self.assertEqual(read_shift(reopened, 'S'), sealed)
        self.assertEqual(seal_shift(reopened, 'S'), sealed)
        self.assertEqual(read_shift(reopened, 'S')['receipt'], sealed['receipt'])

    def test_empty_shift_seals_with_no_events(self):
        open_shift(self.stores, 'day', 'E')
        close_shift(self.stores, 'E')
        sealed = seal_shift(self.stores, 'E')
        self.scenario('shiftbook.empty',
                      {'events': [], 'balance': {**ZERO, 'available': 2, 'missing': 1,
                                                 'ineligible': 1}},
                      {'events': sealed['events'], 'balance': sealed['closing_balance']},
                      'A shift opened and closed without changes still seals an empty statement.')
        self.assertEqual(sealed['opening_balance'], sealed['closing_balance'])
        self.assertEqual(sealed['status'], 'sealed')

    def test_unknown_conflict_and_not_closed_rejections(self):
        self.assertEqual(open_shift(self.stores, 'ghost', 'S'), {'status': 'unknown_batch'})
        self.assertEqual(open_shift(self.stores, 'day', 'S'),
                         {'status': 'open', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(open_shift(self.stores, 'neighbor', 'S'), {'status': 'shift_conflict'})
        self.assertEqual(open_shift(self.stores, 'day', 'S'),
                         {'status': 'open', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(seal_shift(self.stores, 'S'), {'status': 'not_closed'})
        self.assertEqual(read_shift(self.stores, 'S'), {'status': 'not_sealed', 'shift_id': 'S'})
        self.assertEqual(close_shift(self.stores, 'ghost'), {'status': 'unknown_shift'})
        self.assertEqual(seal_shift(self.stores, 'ghost'), {'status': 'unknown_shift'})
        self.assertEqual(read_shift(self.stores, 'ghost'), {'status': 'unknown_shift'})
        close_shift(self.stores, 'S')
        self.assertEqual(open_shift(self.stores, 'day', 'S'),
                         {'status': 'closed', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(open_shift(self.stores, 'neighbor', 'S'), {'status': 'shift_conflict'})

    def test_invalid_labels_raise(self):
        for bad in ('', '   ', None, 'a\x00b'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                open_shift(self.stores, 'day', bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                open_shift(self.stores, bad, 'S')
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                close_shift(self.stores, bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                seal_shift(self.stores, bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                read_shift(self.stores, bad)

    def test_retries_and_release_free_cancels_leave_no_events(self):
        open_shift(self.stores, 'day', 'S')
        only_c = (RequestSlot('N0', (self.refs['C'],)),)
        self.assertEqual(sign(self.stores, 'neighbor', 'C1', only_c)['status'], 'signed')
        self.assertEqual(sign(self.stores, 'neighbor', 'C1', only_c)['status'], 'signed')
        impossible = (RequestSlot('U', (self.refs['B'],)), RequestSlot('V', (self.refs['B'],)))
        self.assertEqual(sign(self.stores, 'day', 'Z', impossible)['status'], 'not_enough')
        self.assertEqual(cancel(self.stores, 'day', 'Z', impossible)['status'], 'cancelled')
        self.assertEqual(cancel(self.stores, 'day', 'Z', impossible)['status'], 'cancelled')
        close_shift(self.stores, 'S')
        sealed = seal_shift(self.stores, 'S')
        self.scenario('shiftbook.no_foreign_or_empty_changes', [], sealed['events'],
                      'C is outside the day batch, the C1 retry committed nothing new, and Z '
                      'never held evidence, so the statement has no entries.')

    def test_signed_retry_does_not_duplicate_events(self):
        open_shift(self.stores, 'day', 'S')
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y)['status'], 'signed')
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y)['status'], 'signed')
        close_shift(self.stores, 'S')
        sealed = seal_shift(self.stores, 'S')
        self.assertEqual([[e['kind'], e['request_id']] for e in sealed['events']],
                         [['hold', 'Y'], ['issue', 'Y']])

    def test_pre_open_hold_enters_opening_and_release_is_listed(self):
        self.assertEqual(self.prepare_x()['status'], 'pending')
        open_shift(self.stores, 'day', 'S')
        self.assertEqual(cancel(self.stores, 'neighbor', 'X', self.x)['status'], 'cancelled')
        close_shift(self.stores, 'S')
        sealed = seal_shift(self.stores, 'S')
        self.scenario('shiftbook.pre_open_hold',
                      {'opening': {**ZERO, 'available': 1, 'held': 2, 'missing': 1},
                       'closing': {**ZERO, 'available': 2, 'missing': 1, 'ineligible': 1},
                       'events': [['release', 'X']]},
                      {'opening': sealed['opening_balance'], 'closing': sealed['closing_balance'],
                       'events': [[e['kind'], e['request_id']] for e in sealed['events']]},
                      'The neighbor hold predates S, so it sits in the opening balance; only '
                      'its release falls inside the interval.')
        self.assertEqual(sealed['events'][0]['lines'], self.x_lines())

    def test_hold_before_open_issues_within_interval(self):
        self.assertEqual(self.prepare_y()['status'], 'pending')
        open_shift(self.stores, 'day', 'S')
        self.assertEqual(sign(self.stores, 'day', 'Y', self.y)['status'], 'signed')
        close_shift(self.stores, 'S')
        sealed = seal_shift(self.stores, 'S')
        self.assertEqual([[e['kind'], e['report_id']] for e in sealed['events']],
                         [['issue', 'R-Y']])
        self.assertEqual(sealed['opening_balance']['held'], 1)
        self.assertEqual(sealed['closing_balance']['used'], 1)

    def test_pre_upgrade_store_keeps_data_and_current_facts_enter_opening(self):
        self.assertEqual(self.prepare_y()['status'], 'pending')
        with custody_transaction(self.stores.ledger, write=True) as db:
            db.execute('DROP TABLE custody_events')
            db.execute('DROP TABLE shifts')
        with archive_transaction(self.stores.archive, write=True) as db:
            db.execute('DROP TABLE shift_statements')
        stores = initialize(self.directory)
        self.assertEqual(open_shift(stores, 'day', 'S'),
                         {'status': 'open', 'batch_id': 'day', 'shift_id': 'S'})
        self.assertEqual(sign(stores, 'day', 'Y', self.y)['status'], 'signed')
        close_shift(stores, 'S')
        sealed = seal_shift(stores, 'S')
        self.scenario('shiftbook.pre_upgrade_store',
                      {'opening_held': 1, 'events': [['issue', 'Y', 'R-Y']], 'closing_used': 1},
                      {'opening_held': sealed['opening_balance']['held'],
                       'events': [[e['kind'], e['request_id'], e['report_id']]
                                  for e in sealed['events']],
                       'closing_used': sealed['closing_balance']['used']},
                      'The pre-upgrade hold was never logged, so it enters the opening balance '
                      'by current facts; only the post-upgrade issue is listed.')

    def test_locked_stores_return_pending(self):
        open_shift(self.stores, 'day', 'S')
        with self.blocked_ledger():
            self.assertEqual(open_shift(self.stores, 'day', 'T'),
                             {'status': 'pending', 'shift_id': 'T'})
            self.assertEqual(close_shift(self.stores, 'S'), {'status': 'pending', 'shift_id': 'S'})
            self.assertEqual(seal_shift(self.stores, 'S'), {'status': 'pending', 'shift_id': 'S'})
            self.assertEqual(read_shift(self.stores, 'S'), {'status': 'pending', 'shift_id': 'S'})
        self.assertEqual(open_shift(self.broken_archive_stores(), 'day', 'T'),
                         {'status': 'pending', 'shift_id': 'T'})
        self.assertEqual(read_shift(self.broken_archive_stores(), 'S'),
                         {'status': 'pending', 'shift_id': 'S'})
        close_shift(self.stores, 'S')
        with self.blocked_archive():
            self.assertEqual(seal_shift(self.stores, 'S'), {'status': 'pending', 'shift_id': 'S'})
            self.assertEqual(read_shift(self.stores, 'S'), {'status': 'pending', 'shift_id': 'S'})
        self.assertEqual(read_shift(self.stores, 'S'), {'status': 'not_sealed', 'shift_id': 'S'})


if __name__ == '__main__':
    unittest.main()
