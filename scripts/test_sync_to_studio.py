import unittest

from sync_to_studio import first_diff, local_lines


class SourceVerificationTests(unittest.TestCase):
    def test_studio_line_endings_do_not_report_source_drift(self):
        lf = b'--!strict\nreturn { value = 1 }\n'
        crlf = lf.replace(b'\n', b'\r\n')
        self.assertIsNone(first_diff(local_lines(lf), local_lines(crlf)))

    def test_content_changes_still_report_drift(self):
        studio = local_lines(b'return 1\n')
        authored = local_lines(b'return 2\r\n')
        self.assertEqual(first_diff(studio, authored), (1, 'return 1', 'return 2'))


if __name__ == '__main__':
    unittest.main()
