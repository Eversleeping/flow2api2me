import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services import browser_login_profiles as profiles


class LoginProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(profiles, "PROFILE_ROOT", self.root)
        self.patch.start()
        self.profile = self.root / "session-saved"
        self.profile.mkdir()
        self.token = types.SimpleNamespace(id=1, email="user@example.com")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_only_same_account_can_resume_saved_profile(self):
        profiles.retain_login_profile(self.profile, self.token)
        self.assertTrue(profiles.is_retained_login_profile(self.profile))
        with patch.object(profiles, "_profile_in_use", return_value=False):
            self.assertEqual(profiles.find_retained_login_profile(1, "USER@example.com"), str(self.profile))
            self.assertIsNone(profiles.find_retained_login_profile(2, self.token.email))
            self.assertIsNone(profiles.find_retained_login_profile(1, "other@example.com"))
        self.assertNotIn(self.token.email, (self.profile / profiles.MARKER).read_text())

    def test_active_profile_cannot_be_opened_again(self):
        profiles.retain_login_profile(self.profile, self.token)
        with patch.object(profiles, "_profile_in_use", return_value=True):
            self.assertIsNone(profiles.find_retained_login_profile(1, self.token.email))

    def test_symlink_cannot_select_another_profile(self):
        profiles.retain_login_profile(self.profile, self.token)
        link = self.root / "session-link"
        link.symlink_to(self.profile, target_is_directory=True)
        self.assertFalse(profiles.is_retained_login_profile(link))
        (self.profile / profiles.MARKER).unlink()
        (self.root / "metadata").write_text(json.dumps({"token_id": 1}))
        (self.profile / profiles.MARKER).symlink_to(self.root / "metadata")
        self.assertFalse(profiles.is_retained_login_profile(self.profile))


if __name__ == "__main__":
    unittest.main()
