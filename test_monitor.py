import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import monitor as m


def record(pid='253', title='LAX.AS3.Pro.TINY', stock=True, **prices):
    return dict(pid=pid, title=title, in_stock=stock, prices=prices,
                amount='10.90', currency='USD', cycle='Monthly')


class MonitorTests(unittest.TestCase):
    def test_only_west_coast_premium(self):
        self.assertTrue(m.in_scope('LAX.AS3.Pro.TINY'))
        self.assertTrue(m.in_scope('SJC.AN5.Pro.MINI'))
        for name in ['LAX.AS3.EB.TINY', 'LAX.AN5.T1.MINI', 'TYO.AS3.Pro.TINY', 'HKG.AS3.Pro.TINY']:
            self.assertFalse(m.in_scope(name))

    def test_strict_price_limits_and_independent_billing(self):
        self.assertEqual(m.qualifying_prices({'monthly': '20', 'annually': '100'}), {})
        self.assertEqual(m.qualifying_prices({'monthly': '19.99', 'annually': '99.99'}),
                         {'monthly': '19.99', 'annually': '99.99'})
        self.assertEqual(m.qualifying_prices({'monthly': '25', 'annually': '80'}), {'annually': '80'})
        self.assertEqual(m.qualifying_prices({'monthly': '10', 'annually': '150'}), {'monthly': '10'})

    def test_no_monthly_conversion_or_other_cycles(self):
        prices = m.parse_prices([{'value': 'monthly', 'text': '$10.90 USD Monthly'},
                                 {'value': 'quarterly', 'text': '$29.99 USD Quarterly'},
                                 {'value': 'annually', 'text': '$109.00 USD Annually'}])
        self.assertEqual(prices, {'monthly': '10.90', 'annually': '109.00'})
        self.assertEqual(m.qualifying_prices(prices), {'monthly': '10.90'})

    def test_currency_and_disabled_options(self):
        for text in ['$8.00 CAD Annually', '$8.00 Monthly', '$0.00 USD Monthly']:
            with self.assertRaises(m.CheckError):
                m.parse_amount(text)
        self.assertEqual(m.parse_prices([{'value': 'annually', 'text': '$80.00 USD Annually'},
                                         {'value': 'monthly', 'text': '$2.00 USD Monthly', 'disabled': True}]),
                         {'annually': '80.00'})
        self.assertEqual(m.parse_amount('$1,099.00 USD Annually'), '1099.00')

    def test_dedupe_restock_and_price_change(self):
        state = {'products': {}}
        push = Mock()
        offer = record(monthly='10.90')
        m.apply_records(state, [offer], push)
        m.apply_records(state, [offer], push)
        self.assertEqual(push.call_count, 1)
        m.apply_records(state, [record(stock=False)], push)
        m.apply_records(state, [offer], push)
        self.assertEqual(push.call_count, 2)
        m.apply_records(state, [record(monthly='9.90')], push)
        self.assertEqual(push.call_count, 3)

    def test_unknown_and_missing_do_not_rearm(self):
        state = {'products': {}}
        push = Mock()
        offer = record(monthly='10.90')
        m.apply_records(state, [offer], push)
        m.apply_records(state, [{**offer, 'error': 'HTTP 403'}], push)
        m.apply_records(state, [], push)
        m.apply_records(state, [offer], push)
        self.assertEqual(push.call_count, 1)

    def test_failed_push_remains_retryable(self):
        state = {'products': {}}
        offer = record(monthly='10.90')
        with self.assertRaises(m.CheckError):
            m.apply_records(state, [offer], Mock(side_effect=m.CheckError('failed')))
        self.assertNotIn('253', state['products'])
        push = Mock()
        m.apply_records(state, [offer], push)
        push.assert_called_once()

    def test_critical_payload(self):
        p = m.push_payload('test', 'test')
        self.assertEqual((p['level'], p['volume'], p['call'], p['sound']), ('critical', '10', '1', 'alarm'))
        self.assertNotIn('call', m.push_payload('error', 'error', critical=False))

    def test_copied_bark_url_and_destination(self):
        with patch.dict('os.environ', {'BARK_KEY': 'https://api.day.app/AbCd123456/test'}, clear=False):
            self.assertEqual(m.validate_push_config()[0], 'AbCd123456')
        with patch.dict('os.environ', {'BARK_KEY': 'https://another.example/AbCd123456/test'}, clear=False):
            with self.assertRaises(m.CheckError):
                m.validate_push_config()

    def test_three_failures_alert_once_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            fail = Mock(side_effect=m.CheckError('HTTP 403'))
            notify = Mock()
            for _ in range(4):
                self.assertEqual(m.run(collector=fail, notifier=notify, state_path=path), 1)
            notify.assert_called_once()
            self.assertEqual(m.load_state(path)['failures'], 4)
            success = lambda: ([record(stock=False)], [])
            self.assertEqual(m.run(collector=success, notifier=notify, state_path=path), 0)
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(m.load_state(path)['failures'], 0)

    def test_check_does_not_consume_stock_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            notify = Mock()
            collect = lambda: ([record(monthly='10.90')], [])
            self.assertEqual(m.run(True, collect, notify, path), 0)
            notify.assert_not_called()
            self.assertEqual(m.run(False, collect, notify, path), 0)
            notify.assert_called_once()

    def test_first_success_persisted_when_second_push_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            offers = [record(monthly='10.90'), record(pid='254', monthly='16.90')]
            notify = Mock(side_effect=[None, m.CheckError('failed')])
            self.assertEqual(m.run(False, lambda: (offers, []), notify, path), 1)
            saved = m.load_state(path)
            self.assertTrue(saved['products']['253']['notified'])
            self.assertNotIn('254', saved['products'])

    def test_empty_catalog_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(m.run(True, lambda: ([], []), Mock(), Path(directory) / 'state.json'), 1)


if __name__ == '__main__':
    unittest.main()
