# Copyright (c) 2013 Potential Ventures Ltd
# Copyright (c) 2013 SolarFlare Communications Inc
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of Potential Ventures Ltd,
#       SolarFlare Communications Inc nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL POTENTIAL VENTURES LTD BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import sys
import time
import logging
import functools
import inspect
import os

import cocotb
from cocotb.log import SimLog
from cocotb.result import ReturnValue
from cocotb.utils import get_sim_time, lazy_property, remove_traceback_frames
from cocotb import outcomes

# Sadly the Python standard logging module is very slow so it's better not to
# make any calls by testing a boolean flag first
if "COCOTB_SCHEDULER_DEBUG" in os.environ:
    _debug = True
else:
    _debug = False


def public(f):
    """Use a decorator to avoid retyping function/class names.

    * Based on an idea by Duncan Booth:
    http://groups.google.com/group/comp.lang.python/msg/11cbb03e09611b8a
    * Improved via a suggestion by Dave Angel:
    http://groups.google.com/group/comp.lang.python/msg/3d400fb22d8a42e1
    """
    all = sys.modules[f.__module__].__dict__.setdefault('__all__', [])
    if f.__name__ not in all:  # Prevent duplicates if run from an IDE.
        all.append(f.__name__)
    return f


public(public)  # Emulate decorating ourself


@public
class CoroutineComplete(Exception):
    """To ensure that a coroutine has completed before we fire any triggers
    that are blocked waiting for the coroutine to end, we create a sub-class
    exception that the scheduler catches and the callbacks are attached
    here.
    """

    def __init__(self, text=""):
        Exception.__init__(self, text)


class RunningTask:
    """Per instance wrapper around a running generator.

    Provides the following:

        task.join() creates a Trigger that will fire when this coroutine
        completes.

        task.kill() will destroy a coroutine instance (and cause any Join
        triggers to fire.
    """

    def __init__(self, inst):

        if inspect.iscoroutine(inst):
            self._natively_awaitable = True
        elif inspect.isgenerator(inst):
            self._natively_awaitable = False
        elif sys.version_info >= (3, 6) and inspect.isasyncgen(inst):
            raise TypeError(
                "{} is an async generator, not a coroutine. "
                "You likely used the yield keyword instead of await.".format(
                    inst.__qualname__))
        else:
            raise TypeError(
                "%s isn't a valid coroutine! Did you forget to use the yield keyword?" % inst)
        self._coro = inst
        self.__name__ = inst.__name__
        self.__qualname__ = inst.__qualname__
        self._started = False
        self._callbacks = []
        self._outcome = None

    @lazy_property
    def log(self):
        # Creating a logger is expensive, only do it if we actually plan to
        # log anything
        return SimLog("cocotb.coroutine.%s" % self.__qualname__, id(self))

    @property
    def retval(self):
        if self._outcome is None:
            raise RuntimeError("coroutine is not complete")
        return self._outcome.get()

    @property
    def _finished(self):
        return self._outcome is not None

    def __iter__(self):
        return self

    def __str__(self):
        return str(self.__qualname__)

    def _advance(self, outcome):
        """Advance to the next yield in this coroutine.

        Args:
            outcome: The :any:`outcomes.Outcome` object to resume with.

        Returns:
            The object yielded from the coroutine

        Raises:
            CoroutineComplete: If the coroutine returns or throws an error, self._outcome is set, and
           :exc:`CoroutineComplete` is thrown.
        """
        try:
            self._started = True
            return outcome.send(self._coro)
        except ReturnValue as e:
            self._outcome = outcomes.Value(e.retval)
            raise CoroutineComplete()
        except StopIteration as e:
            self._outcome = outcomes.Value(e.value)
            raise CoroutineComplete()
        except BaseException as e:
            self._outcome = outcomes.Error(remove_traceback_frames(e, ['_advance', 'send']))
            raise CoroutineComplete()

    def send(self, value):
        return self._coro.send(value)

    def throw(self, exc):
        return self._coro.throw(exc)

    def close(self):
        return self._coro.close()

    def kill(self):
        """Kill a coroutine."""
        if self._outcome is not None:
            # already finished, nothing to kill
            return

        if _debug:
            self.log.debug("kill() called on coroutine")
        # todo: probably better to throw an exception for anyone waiting on the coroutine
        self._outcome = outcomes.Value(None)
        cocotb.scheduler.unschedule(self)

    def join(self):
        """Return a trigger that will fire when the wrapped coroutine exits."""
        return cocotb.triggers.Join(self)

    def has_started(self):
        return self._started

    def __bool__(self):
        """Provide boolean testing
            if the coroutine has finished return false
            otherwise return true"""
        return not self._finished

    def __await__(self):
        # It's tempting to use `return (yield from self._coro)` here,
        # which bypasses the scheduler. Unfortunately, this means that
        # we can't keep track of the result or state of the coroutine,
        # things which we expose in our public API. If you want the
        # efficiency of bypassing the scheduler, remove the `@coroutine`
        # decorator from your `async` functions.

        # Hand the coroutine back to the scheduler trampoline.
        return (yield self)


class RunningCoroutine(RunningTask):
    """
    The result of calling a :any:`cocotb.coroutine` decorated coroutine.

    All this class does is provide some extra attributes.
    """

    def __init__(self, inst, parent):
        RunningTask.__init__(self, inst)
        self._parent = parent
        self.__doc__ = parent._func.__doc__
        self.module = parent._func.__module__
        self.funcname = parent._func.__name__


class RunningTest(RunningCoroutine):
    """Add some useful Test functionality to a RunningCoroutine."""

    class ErrorLogHandler(logging.Handler):
        def __init__(self, fn):
            self.fn = fn
            logging.Handler.__init__(self, level=logging.DEBUG)

        def handle(self, record):
            # For historical reasons, only logs sent directly to the `cocotb`
            # logger are recorded - logs to `cocotb.scheduler` for instance
            # are not recorded. Changing this behavior may have significant
            # memory usage implications, so should not be done without some
            # thought.
            if record.name == 'cocotb':
                self.fn(self.format(record))

    def __init__(self, inst, parent):
        self.error_messages = []
        RunningCoroutine.__init__(self, inst, parent)
        self.log = SimLog("cocotb.test.%s" % self.__qualname__, id(self))
        self.started = False
        self.start_time = 0
        self.start_sim_time = 0
        self.expect_fail = parent.expect_fail
        self.expect_error = parent.expect_error
        self.skip = parent.skip
        self.stage = parent.stage
        self._id = parent._id

        # make sure not to create a circular reference here
        self.handler = RunningTest.ErrorLogHandler(self.error_messages.append)

    def _advance(self, outcome):
        if not self.started:
            self.log.info("Starting test: \"%s\"\nDescription: %s" %
                          (self.funcname, self.__doc__))
            self.start_time = time.time()
            self.start_sim_time = get_sim_time('ns')
            self.started = True
        return super(RunningTest, self)._advance(outcome)

    # like RunningTask.kill(), but with a way to inject a failure
    def abort(self, exc):
        """Force this test to end early, without executing any cleanup.

        This happens when a background task fails, and is consistent with
        how the behavior has always been. In future, we may want to behave
        more gracefully to allow the test body to clean up.

        `exc` is the exception that the test should report as its reason for
        aborting.
        """
        assert self._outcome is None
        outcome = outcomes.Error(exc)
        if _debug:
            self.log.debug("outcome forced to {}".format(outcome))
        self._outcome = outcome
        cocotb.scheduler.unschedule(self)

    def sort_name(self):
        if self.stage is None:
            return "%s.%s" % (self.module, self.funcname)
        else:
            return "%s.%d.%s" % (self.module, self.stage, self.funcname)


class coroutine:
    """Decorator class that allows us to provide common coroutine mechanisms:

    ``log`` methods will log to ``cocotb.coroutine.name``.

    :meth:`~cocotb.decorators.RunningTask.join` method returns an event which will fire when the coroutine exits.

    Used as ``@cocotb.coroutine``.
    """

    def __init__(self, func):
        self._func = func
        functools.update_wrapper(self, func)

    @lazy_property
    def log(self):
        return SimLog("cocotb.coroutine.%s" % self._func.__qualname__, id(self))

    def __call__(self, *args, **kwargs):
        return RunningCoroutine(self._func(*args, **kwargs), self)

    def __get__(self, obj, owner=None):
        """Permit the decorator to be used on class methods
            and standalone functions"""
        return type(self)(self._func.__get__(obj, owner))

    def __iter__(self):
        return self

    def __str__(self):
        return str(self._func.__qualname__)


@public
class function:
    """Decorator class that allows a function to block.

    This allows a coroutine that consumes simulation time
    to be called by a thread started with :class:`cocotb.external`;
    in other words, to internally block while externally
    appear to yield.
    """

    def __init__(self, func):
        self._coro = cocotb.coroutine(func)

    @lazy_property
    def log(self):
        return SimLog("cocotb.function.%s" % self._coro.__qualname__, id(self))

    def __call__(self, *args, **kwargs):
        return cocotb.scheduler.queue_function(self._coro(*args, **kwargs))

    def __get__(self, obj, owner=None):
        """Permit the decorator to be used on class methods
            and standalone functions"""
        return type(self)(self._coro._func.__get__(obj, owner))


@public
class external:
    """Decorator to apply to an external function to enable calling from cocotb.

    This turns a normal function that isn't a coroutine into a blocking coroutine.
    Currently, this creates a new execution thread for each function that is
    called.
    Scope for this to be streamlined to a queue in future.
    """

    def __init__(self, func):
        self._func = func
        self._log = SimLog("cocotb.external.%s" % self._func.__qualname__, id(self))

    def __call__(self, *args, **kwargs):
        return cocotb.scheduler.run_in_executor(self._func, *args, **kwargs)

    def __get__(self, obj, owner=None):
        """Permit the decorator to be used on class methods
            and standalone functions"""
        return type(self)(self._func.__get__(obj, owner))


class _decorator_helper(type):
    """
    Metaclass that allows a type to be constructed using decorator syntax,
    passing the decorated function as the first argument.

    So:

        @MyClass(construction, args='go here')
        def this_is_passed_as_f(...):
            pass

    ends up calling

        MyClass.__init__(this_is_passed_as_f, construction, args='go here')
    """
    def __call__(cls, *args, **kwargs):
        def decorator(f):
            # fall back to the normal way of constructing an object, now that
            # we have all the arguments
            return type.__call__(cls, f, *args, **kwargs)
        return decorator


@public
class hook(coroutine, metaclass=_decorator_helper):
    """Decorator to mark a function as a hook for cocotb.

    Used as ``@cocotb.hook()``.

    All hooks are run at the beginning of a cocotb test suite, prior to any
    test code being run."""

    def __init__(self, f):
        super(hook, self).__init__(f)
        self.im_hook = True
        self.name = self._func.__name__


@public
class test(coroutine, metaclass=_decorator_helper):
    """Decorator to mark a function as a test.

    All tests are coroutines.  The test decorator provides
    some common reporting etc., a test timeout and allows
    us to mark tests as expected failures.

    Tests are evaluated in the order they are defined in a test module.

    Used as ``@cocotb.test(...)``.

    Args:
        timeout_time (numbers.Real or decimal.Decimal, optional):
            Simulation time duration before timeout occurs.

            .. versionadded:: 1.3

            .. note::
                Test timeout is intended for protection against deadlock.
                Users should use :class:`~cocotb.triggers.with_timeout` if they require a
                more general-purpose timeout mechanism.
        timeout_unit (str or None, optional):
            Units of timeout_time, accepts any units that :class:`~cocotb.triggers.Timer` does.

            .. versionadded:: 1.3
        expect_fail (bool, optional):
            Don't mark the result as a failure if the test fails.
        expect_error (bool or exception type or tuple of exception types, optional):
            If ``True``, consider this test passing if it raises *any* :class:`Exception`, and failing if it does not.
            If given an exception type or tuple of exception types, catching *only* a listed exception type is considered passing.
            This is primarily for cocotb internal regression use for when a simulator error is expected.

            Users are encouraged to use the following idiom instead::

                @cocotb.test()
                async def my_test(dut):
                    try:
                        await thing_that_should_fail()
                    except ExceptionIExpect:
                        pass
                    else:
                        assert False, "Exception did not occur"

            .. versionchanged:: 1.3
                Specific exception types can be expected
        skip (bool, optional):
            Don't execute this test as part of the regression.
        stage (int, optional)
            Order tests logically into stages, where multiple tests can share a stage.
    """

    _id_count = 0  # used by the RegressionManager to sort tests in definition order

    def __init__(self, f, timeout_time=None, timeout_unit=None,
                 expect_fail=False, expect_error=False,
                 skip=False, stage=None):

        self._id = self._id_count
        type(self)._id_count += 1

        if timeout_time is not None:
            co = coroutine(f)

            @functools.wraps(f)
            async def f(*args, **kwargs):
                running_co = co(*args, **kwargs)

                try:
                    res = await cocotb.triggers.with_timeout(running_co, self.timeout_time, self.timeout_unit)
                except cocotb.result.SimTimeoutError:
                    running_co.kill()
                    raise
                else:
                    return res

        super(test, self).__init__(f)

        self.timeout_time = timeout_time
        self.timeout_unit = timeout_unit
        self.expect_fail = expect_fail
        if expect_error is True:
            expect_error = (Exception,)
        elif expect_error is False:
            expect_error = ()
        self.expect_error = expect_error
        self.skip = skip
        self.stage = stage
        self.im_test = True    # For auto-regressions
        self.name = self._func.__name__

    def __call__(self, *args, **kwargs):
        return RunningTest(self._func(*args, **kwargs), self)
