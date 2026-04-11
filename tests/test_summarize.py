import unittest
from unittest.mock import patch
import summarize


class SummarizeConfigTests(unittest.TestCase):
    @patch('summarize.call_openrouter', return_value='hello')
    @patch('summarize.send_discord', return_value=True)
    @patch('summarize.filter_by_hours', return_value=[{
        'id': '1',
        'text': 'tweet',
        'user': {'username': 'alice'},
        'created_at': '2026-04-09T00:00:00+00:00',
    }])
    @patch('summarize.load_window', return_value=[{'id': '1'}])
    @patch('summarize.load_config', return_value={'discord': {'alerts_channel': 'CHAN123'}})
    def test_posts_to_nested_discord_alerts_channel(self, *_mocks):
        with patch('sys.argv', ['summarize.py']):
            summarize.main()
        summarize.send_discord.assert_called_once()
        self.assertEqual(summarize.send_discord.call_args.args[1], 'CHAN123')


if __name__ == '__main__':
    unittest.main()
