"""Run the standalone synthetic journal/pin/Git-config safety cases."""
from pathlib import Path
import subprocess
import sys
import unittest

PROFILE=Path(__file__).resolve().parents[1]

class HeadSyncTests(unittest.TestCase):
    def test_synthetic_transaction_recovery_and_guards(self):
        result=subprocess.run(
            [sys.executable,str(PROFILE/"tests/headsync_cases.py"),
             str(PROFILE/"bin/brew-headsync-upgrade")],
            capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+"\n"+result.stderr)
        lines=result.stdout.strip().splitlines()
        self.assertTrue(lines and lines[-1].startswith("PASS "),result.stdout)
        self.assertFalse(any(line.startswith("FAIL ") for line in lines),result.stdout)

if __name__=="__main__":
    unittest.main()
