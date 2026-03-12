# conftest.py — shared pytest fixtures and configuration

import os
import sys
import pytest

# Ensure scripts/ is always on the path for all test files
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../scripts"))

# Set dummy env vars so imports don't fail during test collection
os.environ.setdefault("GITHUB_TOKEN", "test-token-for-unit-tests")
os.environ.setdefault("SMTP_USER", "test@example.com")
os.environ.setdefault("SMTP_PASS", "test-password")
