import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import gpu_watts as app


class ErrorReportTests(unittest.TestCase):
    """Started with pyw there is no console: errors must reach a file and the screen."""
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': self.folder.name})
        self.env.start()
    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()
    def raise_and_report(self, stderr):
        shown = []
        try:
            1/0
        except ZeroDivisionError as exc:
            with patch('sys.stderr', stderr), patch('tkinter.messagebox.showerror', lambda *a, **k: shown.append(a)):
                app.report_error(type(exc), exc, exc.__traceback__)
        return shown
    def test_without_console_the_error_is_logged_and_shown(self):
        shown = self.raise_and_report(None)
        log = Path(self.folder.name) / 'GPU_Watts' / 'errores.log'
        self.assertIn('ZeroDivisionError', log.read_text(encoding='utf-8'))
        self.assertEqual(len(shown), 1)
        self.assertIn(str(log), shown[0][1])
    def test_errors_are_appended(self):
        self.raise_and_report(None)
        self.raise_and_report(None)
        log = Path(self.folder.name) / 'GPU_Watts' / 'errores.log'
        self.assertEqual(log.read_text(encoding='utf-8').count('ZeroDivisionError'), 2)


if __name__ == '__main__':
    unittest.main()
