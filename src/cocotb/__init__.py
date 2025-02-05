# Copyright (c) 2013 Potential Ventures Ltd
# Copyright (c) 2013 SolarFlare Communications Inc
# All rights reserved.

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

import ast
import inspect
import logging as py_logging
import os
import random
import sys
import time
import warnings
from collections.abc import Coroutine
from enum import auto
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Union, cast

import cocotb._profiling
import cocotb.handle
import cocotb.task
import cocotb.triggers
from cocotb._scheduler import Scheduler
from cocotb._utils import DocEnum
from cocotb.logging import default_config
from cocotb.regression import RegressionManager, RegressionMode
from cocotb.result import TestSuccess

from ._version import __version__

# Things we want in the cocotb namespace
from cocotb._decorators import (  # isort: skip # noqa: F401
    bridge,
    resume,
    test,
    parametrize,
)


log: py_logging.Logger
"""The default cocotb logger."""

_scheduler_inst: Scheduler
"""The global scheduler instance."""

regression_manager: RegressionManager
"""The global regression manager instance."""

argv: List[str]
"""The argument list as seen by the simulator."""

plusargs: Dict[str, Union[bool, str]]
"""A dictionary of "plusargs" handed to the simulation.

See :make:var:`COCOTB_PLUSARGS` for details.
"""

packages: SimpleNamespace
"""A :class:`python:types.SimpleNamespace` of package handles.

This will be populated with handles at test time if packages can be discovered
via the GPI.

.. versionadded:: 2.0
"""

SIM_NAME: str
"""The product information of the running simulator."""

SIM_VERSION: str
"""The version of the running simulator."""

_random_seed: int
"""
The value passed to the Python default random number generator.

See :envvar:`COCOTB_RANDOM_SEED` for details on how the value is computed.
This is guaranteed to hold a value at test time.
"""

top: cocotb.handle.SimHandleBase
r"""
A handle to the :envvar:`COCOTB_TOPLEVEL` entity/module.

This is equivalent to the :term:`DUT` parameter given to cocotb tests, so it can be used wherever that variable can be used.
It is particularly useful for extracting information about the :term:`DUT` in module-level class and function definitions;
and in parameters to :class:`.TestFactory`\ s.
"""

is_simulation: bool = False
"""``True`` if cocotb was loaded in a simulation."""


class SimPhase(DocEnum):
    """A phase of the time step."""

    NORMAL = (auto(), "In the Beginning Of Time Step or a Value Change phase.")
    READ_WRITE = (auto(), "In a ReadWrite phase.")
    READ_ONLY = (auto(), "In a ReadOnly phase.")


sim_phase: SimPhase = SimPhase.NORMAL
"""The current phase of the time step."""


def _setup_logging() -> None:
    default_config()
    global log
    log = py_logging.getLogger(__name__)
    import cocotb.simulator
    from cocotb.logging import _filter_from_c, _log_from_c

    cocotb.simulator.initialize_logger(_log_from_c, _filter_from_c)


def _task_done_callback(task: "cocotb.task.Task[Any]") -> None:
    # if cancelled, do nothing
    if task.cancelled():
        return
    # if there's a Task awaiting this one, don't fail
    if task.complete in cocotb._scheduler_inst._trigger2tasks:
        return
    # if no failure, do nothing
    e = task.exception()
    if e is None:
        return
    # there was a failure and no one is watching, fail test
    elif isinstance(e, (TestSuccess, AssertionError)):
        task.log.info("Test stopped by this task")
        cocotb.regression_manager._abort_test(e)
    else:
        task.log.error("Exception raised by this task")
        cocotb.regression_manager._abort_test(e)


def start_soon(
    coro: "Union[cocotb.task.Task[cocotb.task.ResultType], Coroutine[Any, Any, cocotb.task.ResultType]]",
) -> "cocotb.task.Task[cocotb.task.ResultType]":
    """
    Schedule a coroutine to be run concurrently in a :class:`~cocotb.task.Task`.

    Note that this is not an ``async`` function,
    and the new task will not execute until the calling task yields control.

    Args:
        coro: A task or coroutine to be run.

    Returns:
        The :class:`~cocotb.task.Task` that is scheduled to be run.

    .. versionadded:: 1.6.0
    """
    task = create_task(coro)
    task._add_done_callback(_task_done_callback)
    cocotb._scheduler_inst._schedule_task(task)
    return task


async def start(
    coro: "Union[cocotb.task.Task[cocotb.task.ResultType], Coroutine[Any, Any, cocotb.task.ResultType]]",
) -> "cocotb.task.Task[cocotb.task.ResultType]":
    """
    Schedule a coroutine to be run concurrently, then yield control to allow pending tasks to execute.

    The calling task will resume execution before control is returned to the simulator.

    When the calling task resumes, the newly scheduled task may have completed,
    raised an Exception, or be pending on a :class:`~cocotb.triggers.Trigger`.

    Args:
        coro: A task or coroutine to be run.

    Returns:
        The :class:`~cocotb.task.Task` that has been scheduled and allowed to execute.

    .. versionadded:: 1.6.0
    """
    task = start_soon(coro)
    await cocotb.triggers.NullTrigger()
    return task


def create_task(
    coro: "Union[cocotb.task.Task[cocotb.task.ResultType], Coroutine[Any, Any, cocotb.task.ResultType]]",
) -> "cocotb.task.Task[cocotb.task.ResultType]":
    """
    Construct a coroutine into a :class:`~cocotb.task.Task` without scheduling the task.

    The task can later be scheduled with :func:`cocotb.start` or :func:`cocotb.start_soon`.

    Args:
        coro: An existing task or a coroutine to be wrapped.

    Returns:
        Either the provided :class:`~cocotb.task.Task` or a new Task wrapping the coroutine.

    .. versionadded:: 1.6.0
    """
    if isinstance(coro, cocotb.task.Task):
        return coro
    elif isinstance(coro, Coroutine):
        return cocotb.task.Task(coro)
    elif inspect.iscoroutinefunction(coro):
        raise TypeError(
            f"Coroutine function {coro} should be called prior to being scheduled."
        )
    elif inspect.isasyncgen(coro):
        raise TypeError(
            f"{coro.__qualname__} is an async generator, not a coroutine. "
            "You likely used the yield keyword instead of await."
        )
    else:
        raise TypeError(
            f"Attempt to add an object of type {type(coro)} to the scheduler, "
            f"which isn't a coroutine: {coro!r}\n"
        )


_shutdown_callbacks: List[Callable[[], None]] = []
"""List of callbacks to be called when cocotb shuts down."""


def _register_shutdown_callback(cb: Callable[[], None]) -> None:
    """Register a callback to be called when cocotb shuts down."""
    _shutdown_callbacks.append(cb)


def _shutdown_testbench() -> None:
    """Call all registered shutdown callbacks."""
    for cb in _shutdown_callbacks:
        cb()


def _initialise_testbench(argv_: List[str]) -> None:
    from cocotb import simulator

    simulator.set_sim_event_callback(_sim_event)

    global is_simulation
    is_simulation = True

    global argv
    argv = argv_

    # sys.path normally includes "" (the current directory), but does not appear to when python is embedded.
    # Add it back because users expect to be able to import files in their test directory.
    # TODO: move this to gpi_embed.cpp
    sys.path.insert(0, "")

    _setup_logging()

    # From https://www.python.org/dev/peps/pep-0565/#recommended-filter-settings-for-test-runners
    # If the user doesn't want to see these, they can always change the global
    # warning settings in their test module.
    if not sys.warnoptions:
        warnings.simplefilter("default")

    global SIM_NAME, SIM_VERSION
    SIM_NAME = simulator.get_simulator_product().strip()
    SIM_VERSION = simulator.get_simulator_version().strip()

    cocotb.log.info(f"Running on {SIM_NAME} version {SIM_VERSION}")

    log.info(
        f"Running tests with cocotb v{__version__} from {os.path.dirname(__file__)}"
    )

    cocotb._profiling.initialize()
    _register_shutdown_callback(cocotb._profiling.finalize)

    _process_plusargs()
    _process_packages()
    _setup_random_seed()
    _setup_root_handle()
    _start_user_coverage()
    _setup_regression_manager()

    # setup global scheduler system
    global _scheduler_inst
    _scheduler_inst = Scheduler(test_complete_cb=regression_manager._test_complete)

    # start Regression Manager
    regression_manager.start_regression()


def _sim_event(msg: str) -> None:
    """Function that can be called externally to signal an event."""
    # We simply return here as the simulator will exit
    # so no cleanup is needed
    if regression_manager is not None:
        regression_manager._fail_simulation(msg)
    else:
        log.error(msg)
    _shutdown_testbench()


def _process_plusargs() -> None:
    global plusargs

    plusargs = {}

    for option in cocotb.argv:
        if option.startswith("+"):
            if option.find("=") != -1:
                (name, value) = option[1:].split("=", 1)
                plusargs[name] = value
            else:
                plusargs[option[1:]] = True


def _process_packages() -> None:
    global packages

    pkg_dict = {}

    from cocotb import simulator

    pkgs = simulator.package_iterate()
    if pkgs is None:
        packages = SimpleNamespace()
        return

    for pkg in pkgs:
        handle = cast(cocotb.handle.HierarchyObject, cocotb.handle.SimHandle(pkg))
        name = handle._name

        # Icarus doesn't support named access to package objects:
        # https://github.com/steveicarus/iverilog/issues/1038
        # so we cannot lazily create handles
        if SIM_NAME == "Icarus Verilog":
            handle._discover_all()
        pkg_dict[name] = handle

    packages = SimpleNamespace(**pkg_dict)


def _start_user_coverage() -> None:
    coverage_envvar = os.getenv("COCOTB_USER_COVERAGE")
    if coverage_envvar is None:
        coverage_envvar = os.getenv("COVERAGE")
        if coverage_envvar is not None:
            warnings.warn(
                "COVERAGE is deprecated in favor of COCOTB_USER_COVERAGE",
                DeprecationWarning,
            )
    if coverage_envvar:
        try:
            import coverage
        except ImportError:
            cocotb.log.error(
                "Coverage collection requested but coverage module not available. Install it using `pip install coverage`."
            )
        else:
            config_filepath = os.getenv("COCOTB_COVERAGE_RCFILE")
            if config_filepath is None:
                config_filepath = os.getenv("COVERAGE_RCFILE")
                if config_filepath is not None:
                    warnings.warn(
                        "COVERAGE_RCFILE is deprecated in favor of COCOTB_COVERAGE_RCFILE",
                        DeprecationWarning,
                    )
            if config_filepath is None:
                # Exclude cocotb itself from coverage collection.
                cocotb.log.info(
                    "Collecting coverage of user code. No coverage config file supplied via COCOTB_COVERAGE_RCFILE."
                )
                cocotb_package_dir = os.path.dirname(__file__)
                user_coverage = coverage.coverage(
                    branch=True, omit=[f"{cocotb_package_dir}/*"]
                )
            else:
                cocotb.log.info(
                    "Collecting coverage of user code. Coverage config file supplied."
                )
                # Allow the config file to handle all configuration
                user_coverage = coverage.coverage(config_file=config_filepath)
            user_coverage.start()

            def stop_user_coverage() -> None:
                user_coverage.stop()
                cocotb.log.debug("Writing user coverage data")
                user_coverage.save()

            _register_shutdown_callback(stop_user_coverage)


def _setup_random_seed() -> None:
    global _random_seed

    seed_envvar = os.getenv("COCOTB_RANDOM_SEED")
    if seed_envvar is None:
        seed_envvar = os.getenv("RANDOM_SEED")
        if seed_envvar is not None:
            warnings.warn(
                "RANDOM_SEED is deprecated in favor of COCOTB_RANDOM_SEED",
                DeprecationWarning,
            )
    if seed_envvar is None:
        if "ntb_random_seed" in plusargs:
            plusarg_seed = plusargs["ntb_random_seed"]
            if not isinstance(plusarg_seed, str):
                raise TypeError("ntb_random_seed plusarg is not a valid seed value.")
            seed = ast.literal_eval(plusarg_seed)
            if not isinstance(seed, int):
                raise TypeError("ntb_random_seed plusargs is not a valid seed value.")
            _random_seed = seed
        elif "seed" in plusargs:
            plusarg_seed = plusargs["seed"]
            if not isinstance(plusarg_seed, str):
                raise TypeError("seed plusarg is not a valid seed value.")
            seed = ast.literal_eval(plusarg_seed)
            if not isinstance(seed, int):
                raise TypeError("seed plusargs is not a valid seed value.")
            _random_seed = seed
        else:
            _random_seed = int(time.time())
        log.info("Seeding Python random module with %d", _random_seed)
    else:
        _random_seed = ast.literal_eval(seed_envvar)
        log.info("Seeding Python random module with supplied seed %d", _random_seed)

    random.seed(_random_seed)


def _setup_root_handle() -> None:
    root_name = os.getenv("COCOTB_TOPLEVEL")
    if root_name is not None:
        root_name = root_name.strip()
        if root_name == "":
            root_name = None
        elif "." in root_name:
            # Skip any library component of the toplevel
            root_name = root_name.split(".", 1)[1]

    from cocotb import simulator

    handle = simulator.get_root_handle(root_name)
    if not handle:
        raise RuntimeError(f"Can not find root handle {root_name!r}")

    global top
    top = cocotb.handle.SimHandle(handle)


def _setup_regression_manager() -> None:
    global regression_manager
    regression_manager = RegressionManager()

    # discover tests
    module_str = os.getenv("COCOTB_TEST_MODULES", "")
    if not module_str:
        raise RuntimeError(
            "Environment variable COCOTB_TEST_MODULES, which defines the module(s) to execute, is not defined or empty."
        )
    modules = [s.strip() for s in module_str.split(",") if s.strip()]
    regression_manager.setup_pytest_assertion_rewriting()
    regression_manager.discover_tests(*modules)

    # filter tests
    testcase_str = os.getenv("COCOTB_TESTCASE", "").strip()
    test_filter_str = os.getenv("COCOTB_TEST_FILTER", "").strip()
    if testcase_str and test_filter_str:
        raise RuntimeError("Specify only one of COCOTB_TESTCASE or COCOTB_TEST_FILTER")
    elif testcase_str:
        warnings.warn(
            "TESTCASE is deprecated in favor of COCOTB_TEST_FILTER",
            DeprecationWarning,
        )
        filters = [f"{s.strip()}$" for s in testcase_str.split(",") if s.strip()]
        regression_manager.add_filters(*filters)
        regression_manager.set_mode(RegressionMode.TESTCASE)
    elif test_filter_str:
        regression_manager.add_filters(test_filter_str)
        regression_manager.set_mode(RegressionMode.TESTCASE)
