"""Test the AI Office agent adapter."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_office import OpenCodeExecutor, extract_result_payload


class TestOpenCodeExecutor:
    """Test the OpenCode executor."""

    def test_build_command(self):
        executor = OpenCodeExecutor(binary="opencode")
        cmd = executor.build_command("backend", "Implement a feature")
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert cmd[2] == "--agent"
        assert cmd[3] == "backend"

    def test_extract_result_payload_valid(self):
        stdout = '''
Some output
```json
{"status": "complete", "changed_files": ["file1.py"]}
```
'''
        payload = extract_result_payload(stdout)
        assert payload is not None
        assert payload["status"] == "complete"

    def test_extract_result_payload_invalid(self):
        stdout = "No JSON block here"
        payload = extract_result_payload(stdout)
        assert payload is None
