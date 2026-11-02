import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.source_policy import require_redis_membership


class SourcePolicyTests(unittest.TestCase):
    def test_redis_label_alone_does_not_certify_history(self):
        with self.assertRaises(ValueError):
            require_redis_membership({'source': 'Redis QMT'})

    def test_current_snapshot_cannot_replace_history(self):
        with self.assertRaises(ValueError):
            require_redis_membership({'source': 'Redis QMT', 'current_only': True,
                                      'historical_membership_verified': True,
                                      'snapshots': {'20261027': []}})

    def test_external_pool_cannot_be_approved_by_a_history_flag(self):
        with self.assertRaises(ValueError):
            require_redis_membership({'source': 'CSI official anchor and announcement attachments',
                                      'historical_membership_verified': True,
                                      'snapshots': {'20251009': []}})


if __name__ == '__main__':
    unittest.main()
