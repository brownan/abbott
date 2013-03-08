import unittest

from abbott.plugins.auth import satisfies

class TestSatisfies(unittest.TestCase):

    def test_simple(self):
        self.assertTrue(satisfies("admin", "admin"))
        self.assertTrue(satisfies("admin", "admin.foo"))
        self.assertTrue(satisfies("admin", "admin.foo.bar"))

        self.assertFalse(satisfies("admin", "admin2"))
        self.assertFalse(satisfies("admin", "admin2.foo"))
        self.assertFalse(satisfies("admin", "admin2.foo.bar"))

    def test_two_elem(self):
        self.assertTrue(satisfies("admin.foo", "admin.foo"))
        self.assertTrue(satisfies("admin.foo", "admin.foo.bar"))
        self.assertTrue(satisfies("admin.foo", "admin.foo.bar.baz"))

        self.assertFalse(satisfies("admin.foo", "admin"))
        self.assertFalse(satisfies("admin.foo", "admin.bar"))
        self.assertFalse(satisfies("admin.foo", "admin.foobar"))

    def test_glob(self):
        self.assertTrue(satisfies("admin.*", "admin.foo"))
        self.assertTrue(satisfies("admin.*", "admin.foo.bar"))

        self.assertFalse(satisfies("admin.*", "admin"))

    def test_mid_glob(self):
        self.assertTrue(satisfies("admin.*.bar", "admin.foo.bar"))
        self.assertTrue(satisfies("admin.*.bar", "admin.baz.bar"))
        self.assertTrue(satisfies("admin.*.bar", "admin.foo.bar"))

        self.assertFalse(satisfies("admin.*.bar", "admin.bar"))
        self.assertFalse(satisfies("admin.*.bar", "admin.foo.baz.bar"))

    def test_two_globs(self):
        self.assertTrue(satisfies("admin.*.*", "admin.foo.bar"))
        self.assertTrue(satisfies("admin.*.*", "admin.bar.foo"))
        self.assertTrue(satisfies("admin.*.*", "admin.foo.foo.bar"))
        self.assertTrue(satisfies("admin.*.*", "admin.foo.bar.baz.biz"))

    def test_auth_glob(self):
        self.assertTrue(satisfies("admin.groupedit.groupname", "admin.groupedit.*"))
        self.assertTrue(satisfies("admin.groupedit.groupname2", "admin.groupedit.*"))
        self.assertTrue(satisfies("admin.groupedit", "admin.groupedit.*"))
        self.assertTrue(satisfies("admin", "admin.groupedit.*"))

    def test_auth_glob_middle(self):
        self.assertTrue(satisfies("admin.tar.foo", "admin.*.foo"))
        self.assertTrue(satisfies("admin.baz.foo.biz", "admin.*.foo.*"))
        self.assertTrue(satisfies("admin.foo.foo", "admin.*.foo"))
        self.assertTrue(satisfies("admin.foo", "admin.*.foo"))

        self.assertFalse(satisfies("admin.baz.foo.biz", "admin.*.foo"))
        self.assertFalse(satisfies("admin.foo.biz", "admin.*.foo"))
        self.assertFalse(satisfies("admin.foo.bar", "admin.*.foo"))
        self.assertFalse(satisfies("admin.bar.baz", "admin.*.foo"))
        self.assertFalse(satisfies("admin.bar.biz", "admin.*.foo"))

if __name__ == "__main__":
    unittest.main()

