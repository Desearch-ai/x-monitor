import unittest
import monitor


class MonitorPendingAlertTests(unittest.TestCase):
    def test_merge_pending_alerts_preserves_existing_unsent_items(self):
        existing = [
            {'id': '1', 'text': 'older unsent'},
            {'id': '2', 'text': 'still unsent'},
        ]
        new_items = [
            {'id': '2', 'text': 'duplicate'},
            {'id': '3', 'text': 'new alert'},
        ]

        merged = monitor.merge_pending_alerts(existing, new_items)
        self.assertEqual([item['id'] for item in merged], ['1', '2', '3'])


if __name__ == '__main__':
    unittest.main()
