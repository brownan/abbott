from functools import wraps

from twisted.internet import defer
from twisted.trial import unittest

from ..pluginbase import non_reentrant


class TestNonReentrant(unittest.TestCase):

    @non_reentrant(key=2)
    def returns_a_number(self, d, key):

        # A simple deferred chain so that the caller can control when the
        # returned deferred fires
        new_d = defer.Deferred()
        d.addBoth(new_d.callback)
        return new_d

    @defer.inlineCallbacks
    def test_single_entry(self):
        d = defer.Deferred()
        a = self.returns_a_number(d, 1)
        d.callback(5)
        self.assertEquals(5, (yield a))

    @defer.inlineCallbacks
    def test_simple_reentrant(self):
        d1 = defer.Deferred()
        d2 = defer.Deferred()

        r1 = self.returns_a_number(d1, 1)
        r2 = self.returns_a_number(d2, 1)

        # 2 entrances to the function, they should be unified. We expect them
        # to both return the result that is given to d1
        d1.callback(5)
        d2.callback(7)

        self.assertEquals(5, (yield r1))
        self.assertEquals(5, (yield r2))

    @defer.inlineCallbacks
    def test_same_key_kwarg(self):
        d1 = defer.Deferred()
        d2 = defer.Deferred()

        r1 = self.returns_a_number(d1, key=1)
        r2 = self.returns_a_number(d2, key=1)

        # 2 entrances to the function, they should be unified. We expect them
        # to both return the result that is given to d1
        d1.callback(5)
        d2.callback(7)

        self.assertEquals(5, (yield r1))
        self.assertEquals(5, (yield r2))

    @defer.inlineCallbacks
    def test_same_key_mixed(self):
        d1 = defer.Deferred()
        d2 = defer.Deferred()

        r1 = self.returns_a_number(d1, 1)
        r2 = self.returns_a_number(key=1, d=d2)

        # 2 entrances to the function, they should be unified. We expect them
        # to both return the result that is given to d1
        d1.callback(5)
        d2.callback(7)

        self.assertEquals(5, (yield r1))
        self.assertEquals(5, (yield r2))
    
    @defer.inlineCallbacks
    def test_different_keys(self):
        d1 = defer.Deferred()
        d2 = defer.Deferred()

        r1 = self.returns_a_number(d1, 1)
        r2 = self.returns_a_number(d2, 2)

        # 2 entrance to the function but they have different keys. They should
        # return their respective results
        d1.callback(5)
        d2.callback(7)

        self.assertEquals(5, (yield r1))
        self.assertEquals(7, (yield r2))

    @defer.inlineCallbacks
    def test_different_keys_kwargs(self):
        d1 = defer.Deferred()
        d2 = defer.Deferred()

        r1 = self.returns_a_number(d1, key=1)
        r2 = self.returns_a_number(key=2, d=d2)

        # 2 entrance to the function but they have different keys. They should
        # return their respective results
        d1.callback(5)
        d2.callback(7)

        self.assertEquals(5, (yield r1))
        self.assertEquals(7, (yield r2))

