#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Tests for the patch validation logic of the patchset service

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from patchset import Patchset, PatchValidationError  # noqa: E402

VALID_PATCH = b"""\
From: Dev Eloper <dev@kernel.org>
Subject: [PATCH] Makefile: add extraversion

Commit message body.
---
 Makefile | 1 +
 1 file changed, 1 insertion(+)

diff --git a/Makefile b/Makefile
index 1234567..89abcde 100644
--- a/Makefile
+++ b/Makefile
@@ -1,2 +1,3 @@
 VERSION = 6
 PATCHLEVEL = 16
+EXTRAVERSION = -patched
"""


def make_service(tmp_dir, max_size_mb=None):
    service = object.__new__(Patchset)
    service._service_config = types.SimpleNamespace(
        patchset_max_patch_size_mb=max_size_mb,
    )
    return service


class TestPatchValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.checkout = os.path.join(self._tmp.name, "linux-test")
        os.makedirs(self.checkout)
        with open(os.path.join(self.checkout, "Makefile"), "w") as f:
            f.write("VERSION = 6\nPATCHLEVEL = 16\n")
        self.service = make_service(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def validate(self, patch_data):
        self.service._validate_patch("patch0", patch_data, self.checkout)

    def test_valid_patch_accepted(self):
        self.validate(VALID_PATCH)

    def test_path_traversal_rejected(self):
        patch = VALID_PATCH.replace(b"b/Makefile", b"b/../../etc/passwd")
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_absolute_path_rejected(self):
        patch = VALID_PATCH.replace(b"+++ b/Makefile", b"+++ /etc/passwd")
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_git_dir_rejected(self):
        patch = VALID_PATCH.replace(b"b/Makefile", b"b/.git/config")
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_quoted_path_rejected(self):
        patch = VALID_PATCH.replace(b"+++ b/Makefile", b'+++ "b/Makefile"')
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_nul_bytes_rejected(self):
        with self.assertRaises(PatchValidationError):
            self.validate(VALID_PATCH + b"\x00")

    def test_git_binary_patch_rejected(self):
        patch = VALID_PATCH + b"GIT binary patch\nliteral 42\n"
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_binary_files_marker_rejected(self):
        patch = VALID_PATCH + b"Binary files a/blob and b/blob differ\n"
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_symlink_mode_rejected(self):
        patch = VALID_PATCH.replace(
            b"index 1234567..89abcde 100644",
            b"new file mode 120000",
        )
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_submodule_mode_rejected(self):
        patch = VALID_PATCH.replace(
            b"index 1234567..89abcde 100644",
            b"new file mode 160000",
        )
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_ed_style_patch_rejected(self):
        # ed-style diffs have no ---/+++ file headers at all
        with self.assertRaises(PatchValidationError):
            self.validate(b"2a\nmalicious line\n.\n")

    def test_empty_patch_rejected(self):
        with self.assertRaises(PatchValidationError):
            self.validate(b"Just a mail without any diff\n")

    def test_oversized_patch_rejected(self):
        service = make_service(self._tmp.name, max_size_mb=1)
        big = VALID_PATCH + b"#" * (2 * 1024 * 1024)
        with self.assertRaises(PatchValidationError):
            service._validate_patch("patch0", big, self.checkout)

    def test_symlink_escape_rejected(self):
        # In-tree symlink pointing outside the tree must not be a target
        os.symlink("/etc", os.path.join(self.checkout, "escape"))
        patch = VALID_PATCH.replace(b"a/Makefile", b"a/escape/passwd")
        patch = patch.replace(b"b/Makefile", b"b/escape/passwd")
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_rename_outside_tree_rejected(self):
        patch = VALID_PATCH + (
            b"diff --git a/Makefile b/Makefile\n"
            b"rename from Makefile\n"
            b"rename to ../outside\n"
        )
        with self.assertRaises(PatchValidationError):
            self.validate(patch)

    def test_diff_content_inside_hunk_accepted(self):
        # A patch modifying documentation that quotes diff headers must
        # not be misparsed as extra (invalid) file headers
        patch = b"""\
diff --git a/Documentation/diff.rst b/Documentation/diff.rst
index 1234567..89abcde 100644
--- a/Documentation/diff.rst
+++ b/Documentation/diff.rst
@@ -1,2 +1,4 @@
 Example:
 text
+--- /absolute/path
++++ /another/absolute
"""
        self.validate(patch)

    def test_multi_patch_mbox_accepted(self):
        self.validate(VALID_PATCH + b"\n-- \n2.39.1\n\n" + VALID_PATCH)


class TestAllowedDomains(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(Patchset)
        self.service._allowed_domains = {
            "patchwork.kernel.org",
            "files.kernelci.org",
        }

    def test_allowed_domain_accepted(self):
        self.service._has_allowed_domain(
            "https://patchwork.kernel.org/series/1/mbox/"
        )
        self.service._has_allowed_domain(
            "https://files.kernelci.org/custom-patches/x.patch"
        )

    def test_forbidden_domain_rejected(self):
        with self.assertRaises(RuntimeError):
            self.service._has_allowed_domain("https://evil.example.com/x")


if __name__ == "__main__":
    unittest.main()
