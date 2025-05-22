import unittest
import numpy as np
from radiolytics_server.fingerprint_matcher import FingerprintMatcher

class TestFingerprintMatcher(unittest.TestCase):
    def setUp(self):
        self.matcher = FingerprintMatcher()
        # Create a dummy reference fingerprint (N x 4)
        self.reference = [[0.5, 0.5, 0.5, -5.0] for _ in range(20)]
        self.matcher.reference_fingerprints = {
            'TestStation': [(1234567890, 'TestStation', self.reference)]
        }

    def test_true_positive(self):
        # Query identical to reference
        query = [[0.5, 0.5, 0.5, -5.0] for _ in range(20)]
        match = self.matcher._find_best_match(query)
        self.assertIsNotNone(match)
        self.assertEqual(match[1], 'TestStation')
        self.assertGreaterEqual(match[2], self.matcher.MATCH_THRESHOLD)

    def test_false_positive(self):
        # Query very different from reference
        query = [[0.0, 0.0, 0.0, -200.0] for _ in range(20)]
        match = self.matcher._find_best_match(query)
        self.assertIsNone(match)

if __name__ == '__main__':
    unittest.main() 