import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import redact


class TestRedact(unittest.TestCase):
    def test_known_secret_shapes_are_redacted(self):
        samples = [
            "key is sk-ant-api03-AbCdEfGhIjKlMnOp123456",
            "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            "slack xoxb-1234567890-abcdefghijk",
            "aws AKIAIOSFODNN7EXAMPLE",
            "auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "mail nick@example.com ok",
        ]
        for s in samples:
            clean, n = redact.scrub(s)
            self.assertGreaterEqual(n, 1, s)
            self.assertNotIn(s.split()[-2] if "@" in s else s.split()[-1], clean)

    def test_private_key_block(self):
        s = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
        clean, n = redact.scrub(s)
        self.assertEqual(n, 1)
        self.assertNotIn("MIIEow", clean)

    def test_prose_untouched(self):
        s = "Refactor the collector so duplicate reads are counted once per session."
        clean, n = redact.scrub(s)
        self.assertEqual((clean, n), (s, 0))

    def test_high_entropy_run_redacted(self):
        s = "blob Zx9kQ2mP7vR4tY8wB3nH6jL1cF5dG0aS2eK9uI7o"
        clean, n = redact.scrub(s)
        self.assertEqual(n, 1)
        self.assertIn("[REDACTED:entropy]", clean)


if __name__ == "__main__":
    unittest.main()
