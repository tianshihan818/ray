import asyncio
import binascii
from collections import defaultdict
import contextlib
import errno
import functools
import importlib
import inspect
import json
import logging
import multiprocessing
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, unquote, urlparse, parse_qsl, urlunparse
import warnings
from inspect import signature
from pathlib import Path
from subprocess import list2cmdline
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Optional,
    Sequence,
    Tuple,
    Union,
    Coroutine,
    List,
    Mapping,
)

# Import psutil after ray so the packaged version is used.
import psutil
from google.protobuf import json_format

import ray
import ray._private.ray_constants as ray_constants
from ray.core.generated.runtime_env_common_pb2 import (
    RuntimeEnvInfo as ProtoRuntimeEnvInfo,
)

if TYPE_CHECKING:
    from ray.runtime_env import RuntimeEnv

pwd = None
if sys.platform != "win32":
    import pwd

logger = logging.getLogger(__name__)

# Linux can bind child processes' lifetimes to that of their parents via prctl.
# prctl support is detected dynamically once, and assumed thereafter.
linux_prctl = None

# Windows can bind processes' lifetimes to that of kernel-level "job objects".
# We keep a global job object to tie its lifetime to that of our own process.
win32_job = None
win32_AssignProcessToJobObject = None

ENV_DISABLE_DOCKER_CPU_WARNING = "RAY_DISABLE_DOCKER_CPU_WARNING" in os.environ
_PYARROW_VERSION = None

# This global variable is used for testing only
_CALLED_FREQ = defaultdict(lambda: 0)
_CALLED_FREQ_LOCK = threading.Lock()

PLACEMENT_GROUP_INDEXED_BUNDLED_RESOURCE_PATTERN = re.compile(
    r"(.+)_group_(\d+)_([0-9a-zA-Z]+)"
)
PLACEMENT_GROUP_WILDCARD_RESOURCE_PATTERN = re.compile(r"(.+)_group_([0-9a-zA-Z]+)")


def get_user_temp_dir():
    print('[get_user_temp_dir] the parent dir is ', flush=True)
    if "RAY_TMPDIR" in os.environ:
        print('[get_user_temp_dir] set by env var RAY_TMPDIR: ', os.environ["RAY_TMPDIR"], flush=True)
        return os.environ["RAY_TMPDIR"]
    elif sys.platform.startswith("linux") and "TMPDIR" in os.environ:
        print('[get_user_temp_dir] set by env var TMPDIR: ', os.environ["TMPDIR"], flush=True)
        return os.environ["TMPDIR"]
    elif sys.platform.startswith("darwin") or sys.platform.startswith("linux"):
        # Ideally we wouldn't need this fallback, but keep it for now for
        # for compatibility
        tempdir = os.path.join(os.sep, "tmp")
    else:
        tempdir = tempfile.gettempdir()
    print('[get_user_temp_dir] not from env var, automatically: ', tempdir, flush=True)
    return tempdir


def get_ray_temp_dir():
    print('[get_ray_temp_dir] get_ray_temp_dir() calls get_user_temp_dir() to get temp_dir parent dir.', flush=True)
    return os.path.join(get_user_temp_dir(), "ray")


def get_ray_address_file(temp_dir: Optional[str]):
    print('[get_ray_address_file] input temp_dir: ', temp_dir, flush=True)
    if temp_dir is None:
        print('[get_ray_address_file] input temp_dir = None, set it by get_ray_temp_dir().', flush=True)
        temp_dir = get_ray_temp_dir()
        print('[get_ray_address_file] returns temp_dir is: ', temp_dir, flush=True)
    return os.path.join(temp_dir, "ray_current_cluster")


def write_ray_address(ray_address: str, temp_dir: Optional[str] = None):
    # import pdb; pdb.set_trace()
    print('[write_ray_address] call get_ray_address_file(temp_dir) to get address_file path.', flush=True)
    address_file = get_ray_address_file(temp_dir)
    print('[write_ray_address] returns ray address file path is: ', address_file, flush=True)
    if os.path.exists(address_file):
        print('[write_ray_address] if the address file exists', flush=True)
        print('[write_ray_address] read the prev address from it: ', flush=True)
        with open(address_file, "r") as f:
            prev_address = f.read()
        if prev_address == ray_address:
            print('[write_ray_address] if prev_address == current_ray_address, then return, will not trigger Permission Denied ERROR', flush=True)
            return

        logger.info(
            f"Overwriting previous Ray address ({prev_address}). "
            "Running ray.init() on this node will now connect to the new "
            f"instance at {ray_address}. To override this behavior, pass "
            f"address={prev_address} to ray.init()."
        )

    print('[write_ray_address] update ray address in address_file: ', address_file, flush=True)
    ll_cmd = f'ls -l {address_file}'
    print('[write_ray_address] check the permission info of this address_file: ', flush=True)
    print('[write_ray_address] ', subprocess.run(ll_cmd, shell=True), flush=True)
    print('[write_ray_address] check whoami: ', subprocess.run('whoami'), flush=True)
    with open(address_file, "w+") as f:
        f.write(ray_address)


def reset_ray_address(temp_dir: Optional[str] = None):
    address_file = get_ray_address_file(temp_dir)
    if os.path.exists(address_file):
        try:
            os.remove(address_file)
        except OSError:
            pass


def read_ray_address(temp_dir: Optional[str] = None) -> str:
    print('[read_ray_address] input temp_dir: ', temp_dir, flush=True)
    address_file = get_ray_address_file(temp_dir)
    print('[read_ray_address] get address_file path: ', address_file, flush=True)
    if not os.path.exists(address_file):
        return None
    with open(address_file, "r") as f:
        return f.read().strip()


def format_error_message(exception_message: str, task_exception: bool = False):
    """Improve the formatting of an exception thrown by a remote function.

    This method takes a traceback from an exception and makes it nicer by
    removing a few uninformative lines and adding some space to indent the
    remaining lines nicely.

    Args:
        exception_message: A message generated by traceback.format_exc().

    Returns:
        A string of the formatted exception message.
    """
    lines = exception_message.split("\n")
    if task_exception:
        # For errors that occur inside of tasks, remove lines 1 and 2 which are
        # always the same, they just contain information about the worker code.
        lines = lines[0:1] + lines[3:]
        pass
    return "\n".join(lines)


def push_error_to_driver(
    worker, error_type: str, message: str, job_id: Optional[str] = None
):
    """Push an error message to the driver to be printed in the background.

    Args:
        worker: The worker to use.
        error_type: The type of the error.
        message: The message that will be printed in the background
            on the driver.
        job_id: The ID of the driver to push the error message to. If this
            is None, then the message will be pushed to all drivers.
    """
    if job_id is None:
        job_id = ray.JobID.nil()
    assert isinstance(job_id, ray.JobID)
    worker.core_worker.push_error(job_id, error_type, message, time.time())


def publish_error_to_driver(
    error_type: str,
    message: str,
    gcs_publisher,
    job_id=None,
    num_retries=None,
):
    """Push an error message to the driver to be printed in the background.

    Normally the push_error_to_driver function should be used. However, in some
    instances, the raylet client is not available, e.g., because the
    error happens in Python before the driver or worker has connected to the
    backend processes.

    Args:
        error_type: The type of the error.
        message: The message that will be printed in the background
            on the driver.
        gcs_publisher: The GCS publisher to use.
        job_id: The ID of the driver to push the error message to. If this
            is None, then the message will be pushed to all drivers.
    """
    if job_id is None:
        job_id = ray.JobID.nil()
    assert isinstance(job_id, ray.JobID)
    try:
        gcs_publisher.publish_error(
            job_id.hex().encode(), error_type, message, job_id, num_retries
        )
    except Exception:
        logger.exception(f"Failed to publish error: {message} [type {error_type}]")


def decode(byte_str: str, allow_none: bool = False, encode_type: str = "utf-8"):
    """Make this unicode in Python 3, otherwise leave it as bytes.

    Args:
        byte_str: The byte string to decode.
        allow_none: If true, then we will allow byte_str to be None in which
            case we will return an empty string. TODO(rkn): Remove this flag.
            This is only here to simplify upgrading to flatbuffers 1.10.0.

    Returns:
        A byte string in Python 2 and a unicode string in Python 3.
    """
    if byte_str is None and allow_none:
        return ""

    if not isinstance(byte_str, bytes):
        raise ValueError(f"The argument {byte_str} must be a bytes object.")
    return byte_str.decode(encode_type)


def ensure_str(s, encoding="utf-8", errors="strict"):
    """Coerce *s* to `str`.

    - `str` -> `str`
    - `bytes` -> decoded to `str`
    """
    if isinstance(s, str):
        return s
    else:
        assert isinstance(s, bytes)
        return s.decode(encoding, errors)


def binary_to_object_ref(binary_object_ref):
    return ray.ObjectRef(binary_object_ref)


def binary_to_task_id(binary_task_id):
    return ray.TaskID(binary_task_id)


def binary_to_hex(identifier):
    hex_identifier = binascii.hexlify(identifier)
    hex_identifier = hex_identifier.decode()
    return hex_identifier


def hex_to_binary(hex_identifier):
    return binascii.unhexlify(hex_identifier)


# TODO(qwang): Remove these hepler functions
# once we separate `WorkerID` from `UniqueID`.
def compute_job_id_from_driver(driver_id):
    assert isinstance(driver_id, ray.WorkerID)
    return ray.JobID(driver_id.binary()[0 : ray.JobID.size()])


def compute_driver_id_from_job(job_id):
    assert isinstance(job_id, ray.JobID)
    rest_length = ray_constants.ID_SIZE - job_id.size()
    driver_id_str = job_id.binary() + (rest_length * b"\xff")
    return ray.WorkerID(driver_id_str)


def get_visible_accelerator_ids() -> Mapping[str, Optional[List[str]]]:
    """Get the mapping from accelerator resource name
    to the visible ids."""

    from ray._private.accelerators import (
        get_all_accelerator_resource_names,
        get_accelerator_manager_for_resource,
    )

    return {
        accelerator_resource_name: get_accelerator_manager_for_resource(
            accelerator_resource_name
        ).get_current_process_visible_accelerator_ids()
        for accelerator_resource_name in get_all_accelerator_resource_names()
    }


def set_omp_num_threads_if_unset() -> bool:
    """Set the OMP_NUM_THREADS to default to num cpus assigned to the worker

    This function sets the environment variable OMP_NUM_THREADS for the worker,
    if the env is not previously set and it's running in worker (WORKER_MODE).

    Returns True if OMP_NUM_THREADS is set in this function.

    """
    num_threads_from_env = os.environ.get("OMP_NUM_THREADS")
    if num_threads_from_env is not None:
        # No ops if it's set
        return False

    # If unset, try setting the correct CPU count assigned.
    runtime_ctx = ray.get_runtime_context()
    if runtime_ctx.worker.mode != ray._private.worker.WORKER_MODE:
        # Non worker mode, no ops.
        return False

    num_assigned_cpus = runtime_ctx.get_assigned_resources().get("CPU")

    if num_assigned_cpus is None:
        # This is an actor task w/o any num_cpus specified, set it to 1
        logger.debug(
            "[ray] Forcing OMP_NUM_THREADS=1 to avoid performance "
            "degradation with many workers (issue #6998). You can override this "
            "by explicitly setting OMP_NUM_THREADS, or changing num_cpus."
        )
        num_assigned_cpus = 1

    import math

    # For num_cpu < 1: Set to 1.
    # For num_cpus >= 1: Set to the floor of the actual assigned cpus.
    omp_num_threads = max(math.floor(num_assigned_cpus), 1)
    os.environ["OMP_NUM_THREADS"] = str(omp_num_threads)
    return True


last_set_visible_accelerator_ids = {}


def set_visible_accelerator_ids() -> None:
    """Set (CUDA_VISIBLE_DEVICES, NEURON_RT_VISIBLE_CORES, TPU_VISIBLE_CHIPS ,...)
    environment variables based on the accelerator runtime.
    """
    global last_set_visible_accelerator_ids
    for resource_name, accelerator_ids in (
        ray.get_runtime_context().get_resource_ids().items()
    ):
        if last_set_visible_accelerator_ids.get(resource_name, None) == accelerator_ids:
            continue  # optimization: already set
        ray._private.accelerators.get_accelerator_manager_for_resource(
            resource_name
        ).set_current_process_visible_accelerator_ids(accelerator_ids)
        last_set_visible_accelerator_ids[resource_name] = accelerator_ids


def resources_from_ray_options(options_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Determine a task's resource requirements.

    Args:
        options_dict: The dictionary that contains resources requirements.

    Returns:
        A dictionary of the resource requirements for the task.
    """
    resources = (options_dict.get("resources") or {}).copy()

    if "CPU" in resources or "GPU" in resources:
        raise ValueError(
            "The resources dictionary must not contain the key 'CPU' or 'GPU'"
        )
    elif "memory" in resources or "object_store_memory" in resources:
        raise ValueError(
            "The resources dictionary must not "
            "contain the key 'memory' or 'object_store_memory'"
        )

    num_cpus = options_dict.get("num_cpus")
    num_gpus = options_dict.get("num_gpus")
    memory = options_dict.get("memory")
    object_store_memory = options_dict.get("object_store_memory")
    accelerator_type = options_dict.get("accelerator_type")

    if num_cpus is not None:
        resources["CPU"] = num_cpus
    if num_gpus is not None:
        resources["GPU"] = num_gpus
    if memory is not None:
        resources["memory"] = int(memory)
    if object_store_memory is not None:
        resources["object_store_memory"] = object_store_memory
    if accelerator_type is not None:
        resources[
            f"{ray_constants.RESOURCE_CONSTRAINT_PREFIX}{accelerator_type}"
        ] = 0.001

    return resources


class Unbuffered(object):
    """There's no "built-in" solution to programatically disabling buffering of
    text files. Ray expects stdout/err to be text files, so creating an
    unbuffered binary file is unacceptable.

    See
    https://mail.python.org/pipermail/tutor/2003-November/026645.html.
    https://docs.python.org/3/library/functions.html#open

    """

    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()

    def writelines(self, datas):
        self.stream.writelines(datas)
        self.stream.flush()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)


def open_log(path, unbuffered=False, **kwargs):
    """
    Opens the log file at `path`, with the provided kwargs being given to
    `open`.
    """
    # Disable buffering, see test_advanced_3.py::test_logging_to_driver
    kwargs.setdefault("buffering", 1)
    kwargs.setdefault("mode", "a")
    kwargs.setdefault("encoding", "utf-8")
    stream = open(path, **kwargs)
    if unbuffered:
        return Unbuffered(stream)
    else:
        return stream


def get_system_memory(
    # For cgroups v1:
    memory_limit_filename="/sys/fs/cgroup/memory/memory.limit_in_bytes",
    # For cgroups v2:
    memory_limit_filename_v2="/sys/fs/cgroup/memory.max",
):
    """Return the total amount of system memory in bytes.

    Returns:
        The total amount of system memory in bytes.
    """
    # Try to accurately figure out the memory limit if we are in a docker
    # container. Note that this file is not specific to Docker and its value is
    # often much larger than the actual amount of memory.
    docker_limit = None
    if os.path.exists(memory_limit_filename):
        with open(memory_limit_filename, "r") as f:
            docker_limit = int(f.read().strip())
    elif os.path.exists(memory_limit_filename_v2):
        with open(memory_limit_filename_v2, "r") as f:
            # Don't forget to strip() the newline:
            max_file = f.read().strip()
            if max_file.isnumeric():
                docker_limit = int(max_file)
            else:
                # max_file is "max", i.e. is unset.
                docker_limit = None

    # Use psutil if it is available.
    psutil_memory_in_bytes = psutil.virtual_memory().total

    if docker_limit is not None:
        # We take the min because the cgroup limit is very large if we aren't
        # in Docker.
        return min(docker_limit, psutil_memory_in_bytes)

    return psutil_memory_in_bytes


def _get_docker_cpus(
    cpu_quota_file_name="/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
    cpu_period_file_name="/sys/fs/cgroup/cpu/cpu.cfs_period_us",
    cpuset_file_name="/sys/fs/cgroup/cpuset/cpuset.cpus",
    cpu_max_file_name="/sys/fs/cgroup/cpu.max",
) -> Optional[float]:
    # TODO (Alex): Don't implement this logic oursleves.
    # Docker has 2 underyling ways of implementing CPU limits:
    # https://docs.docker.com/config/containers/resource_constraints/#configure-the-default-cfs-scheduler
    # 1. --cpuset-cpus 2. --cpus or --cpu-quota/--cpu-period (--cpu-shares is a
    # soft limit so we don't worry about it). For Ray's purposes, if we use
    # docker, the number of vCPUs on a machine is whichever is set (ties broken
    # by smaller value).

    cpu_quota = None
    # See: https://bugs.openjdk.java.net/browse/JDK-8146115
    if os.path.exists(cpu_quota_file_name) and os.path.exists(cpu_period_file_name):
        try:
            with open(cpu_quota_file_name, "r") as quota_file, open(
                cpu_period_file_name, "r"
            ) as period_file:
                cpu_quota = float(quota_file.read()) / float(period_file.read())
        except Exception:
            logger.exception("Unexpected error calculating docker cpu quota.")
    # Look at cpu.max for cgroups v2
    elif os.path.exists(cpu_max_file_name):
        try:
            max_file = open(cpu_max_file_name).read()
            quota_str, period_str = max_file.split()
            if quota_str.isnumeric() and period_str.isnumeric():
                cpu_quota = float(quota_str) / float(period_str)
            else:
                # quota_str is "max" meaning the cpu quota is unset
                cpu_quota = None
        except Exception:
            logger.exception("Unexpected error calculating docker cpu quota.")
    if (cpu_quota is not None) and (cpu_quota < 0):
        cpu_quota = None
    elif cpu_quota == 0:
        # Round up in case the cpu limit is less than 1.
        cpu_quota = 1

    cpuset_num = None
    if os.path.exists(cpuset_file_name):
        try:
            with open(cpuset_file_name) as cpuset_file:
                ranges_as_string = cpuset_file.read()
                ranges = ranges_as_string.split(",")
                cpu_ids = []
                for num_or_range in ranges:
                    if "-" in num_or_range:
                        start, end = num_or_range.split("-")
                        cpu_ids.extend(list(range(int(start), int(end) + 1)))
                    else:
                        cpu_ids.append(int(num_or_range))
                cpuset_num = len(cpu_ids)
        except Exception:
            logger.exception("Unexpected error calculating docker cpuset ids.")
    # Possible to-do: Parse cgroups v2's cpuset.cpus.effective for the number
    # of accessible CPUs.

    if cpu_quota and cpuset_num:
        return min(cpu_quota, cpuset_num)
    return cpu_quota or cpuset_num


def get_num_cpus(
    override_docker_cpu_warning: bool = ENV_DISABLE_DOCKER_CPU_WARNING,
) -> int:
    """
    Get the number of CPUs available on this node.
    Depending on the situation, use multiprocessing.cpu_count() or cgroups.

    Args:
        override_docker_cpu_warning: An extra flag to explicitly turn off the Docker
            warning. Setting this flag True has the same effect as setting the env
            RAY_DISABLE_DOCKER_CPU_WARNING. By default, whether or not to log
            the warning is determined by the env variable
            RAY_DISABLE_DOCKER_CPU_WARNING.
    """
    cpu_count = multiprocessing.cpu_count()
    if os.environ.get("RAY_USE_MULTIPROCESSING_CPU_COUNT"):
        logger.info(
            "Detected RAY_USE_MULTIPROCESSING_CPU_COUNT=1: Using "
            "multiprocessing.cpu_count() to detect the number of CPUs. "
            "This may be inconsistent when used inside docker. "
            "To correctly detect CPUs, unset the env var: "
            "`RAY_USE_MULTIPROCESSING_CPU_COUNT`."
        )
        return cpu_count
    try:
        # Not easy to get cpu count in docker, see:
        # https://bugs.python.org/issue36054
        docker_count = _get_docker_cpus()
        if docker_count is not None and docker_count != cpu_count:
            # Don't log this warning if we're on K8s or if the warning is
            # explicitly disabled.
            if (
                "KUBERNETES_SERVICE_HOST" not in os.environ
                and not ENV_DISABLE_DOCKER_CPU_WARNING
                and not override_docker_cpu_warning
            ):
                logger.warning(
                    "Detecting docker specified CPUs. In "
                    "previous versions of Ray, CPU detection in containers "
                    "was incorrect. Please ensure that Ray has enough CPUs "
                    "allocated. As a temporary workaround to revert to the "
                    "prior behavior, set "
                    "`RAY_USE_MULTIPROCESSING_CPU_COUNT=1` as an env var "
                    "before starting Ray. Set the env var: "
                    "`RAY_DISABLE_DOCKER_CPU_WARNING=1` to mute this warning."
                )
            # TODO (Alex): We should probably add support for fractional cpus.
            if int(docker_count) != float(docker_count):
                logger.warning(
                    f"Ray currently does not support initializing Ray "
                    f"with fractional cpus. Your num_cpus will be "
                    f"truncated from {docker_count} to "
                    f"{int(docker_count)}."
                )
            docker_count = int(docker_count)
            cpu_count = docker_count

    except Exception:
        # `nproc` and cgroup are linux-only. If docker only works on linux
        # (will run in a linux VM on other platforms), so this is fine.
        pass

    return cpu_count


# TODO(clarng): merge code with c++
def get_cgroupv1_used_memory(filename):
    with open(filename, "r") as f:
        lines = f.readlines()
        cache_bytes = -1
        rss_bytes = -1
        inactive_file_bytes = -1
        working_set = -1
        for line in lines:
            if "total_rss " in line:
                rss_bytes = int(line.split()[1])
            elif "cache " in line:
                cache_bytes = int(line.split()[1])
            elif "inactive_file" in line:
                inactive_file_bytes = int(line.split()[1])
        if cache_bytes >= 0 and rss_bytes >= 0 and inactive_file_bytes >= 0:
            working_set = rss_bytes + cache_bytes - inactive_file_bytes
            assert working_set >= 0
            return working_set
        return None


def get_cgroupv2_used_memory(stat_file, usage_file):
    # Uses same calculation as libcontainer, that is:
    # memory.current - memory.stat[inactive_file]
    # Source: https://github.com/google/cadvisor/blob/24dd1de08a72cfee661f6178454db995900c0fee/container/libcontainer/handler.go#L836  # noqa: E501
    inactive_file_bytes = -1
    current_usage = -1
    with open(usage_file, "r") as f:
        current_usage = int(f.read().strip())
    with open(stat_file, "r") as f:
        lines = f.readlines()
        for line in lines:
            if "inactive_file" in line:
                inactive_file_bytes = int(line.split()[1])
        if current_usage >= 0 and inactive_file_bytes >= 0:
            working_set = current_usage - inactive_file_bytes
            assert working_set >= 0
            return working_set
        return None


def get_used_memory():
    """Return the currently used system memory in bytes

    Returns:
        The total amount of used memory
    """
    # Try to accurately figure out the memory usage if we are in a docker
    # container.
    docker_usage = None
    # For cgroups v1:
    memory_usage_filename = "/sys/fs/cgroup/memory/memory.stat"
    # For cgroups v2:
    memory_usage_filename_v2 = "/sys/fs/cgroup/memory.current"
    memory_stat_filename_v2 = "/sys/fs/cgroup/memory.stat"
    if os.path.exists(memory_usage_filename):
        docker_usage = get_cgroupv1_used_memory(memory_usage_filename)
    elif os.path.exists(memory_usage_filename_v2) and os.path.exists(
        memory_stat_filename_v2
    ):
        docker_usage = get_cgroupv2_used_memory(
            memory_stat_filename_v2, memory_usage_filename_v2
        )

    if docker_usage is not None:
        return docker_usage
    return psutil.virtual_memory().used


def estimate_available_memory():
    """Return the currently available amount of system memory in bytes.

    Returns:
        The total amount of available memory in bytes. Based on the used
        and total memory.

    """
    return get_system_memory() - get_used_memory()


def get_shared_memory_bytes():
    """Get the size of the shared memory file system.

    Returns:
        The size of the shared memory file system in bytes.
    """
    # Make sure this is only called on Linux.
    assert sys.platform == "linux" or sys.platform == "linux2"

    shm_fd = os.open("/dev/shm", os.O_RDONLY)
    try:
        shm_fs_stats = os.fstatvfs(shm_fd)
        # The value shm_fs_stats.f_bsize is the block size and the
        # value shm_fs_stats.f_bavail is the number of available
        # blocks.
        shm_avail = shm_fs_stats.f_bsize * shm_fs_stats.f_bavail
    finally:
        os.close(shm_fd)

    return shm_avail


def check_oversized_function(
    pickled: bytes, name: str, obj_type: str, worker: "ray.Worker"
) -> None:
    """Send a warning message if the pickled function is too large.

    Args:
        pickled: the pickled function.
        name: name of the pickled object.
        obj_type: type of the pickled object, can be 'function',
            'remote function', or 'actor'.
        worker: the worker used to send warning message. message will be logged
            locally if None.
    """
    length = len(pickled)
    if length <= ray_constants.FUNCTION_SIZE_WARN_THRESHOLD:
        return
    elif length < ray_constants.FUNCTION_SIZE_ERROR_THRESHOLD:
        warning_message = (
            "The {} {} is very large ({} MiB). "
            "Check that its definition is not implicitly capturing a large "
            "array or other object in scope. Tip: use ray.put() to put large "
            "objects in the Ray object store."
        ).format(obj_type, name, length // (1024 * 1024))
        if worker:
            push_error_to_driver(
                worker,
                ray_constants.PICKLING_LARGE_OBJECT_PUSH_ERROR,
                "Warning: " + warning_message,
                job_id=worker.current_job_id,
            )
    else:
        error = (
            "The {} {} is too large ({} MiB > FUNCTION_SIZE_ERROR_THRESHOLD={}"
            " MiB). Check that its definition is not implicitly capturing a "
            "large array or other object in scope. Tip: use ray.put() to "
            "put large objects in the Ray object store."
        ).format(
            obj_type,
            name,
            length // (1024 * 1024),
            ray_constants.FUNCTION_SIZE_ERROR_THRESHOLD // (1024 * 1024),
        )
        raise ValueError(error)


def is_main_thread():
    return threading.current_thread().getName() == "MainThread"


def detect_fate_sharing_support_win32():
    global win32_job, win32_AssignProcessToJobObject
    if win32_job is None and sys.platform == "win32":
        import ctypes

        try:
            from ctypes.wintypes import BOOL, DWORD, HANDLE, LPCWSTR, LPVOID

            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.CreateJobObjectW.argtypes = (LPVOID, LPCWSTR)
            kernel32.CreateJobObjectW.restype = HANDLE
            sijo_argtypes = (HANDLE, ctypes.c_int, LPVOID, DWORD)
            kernel32.SetInformationJobObject.argtypes = sijo_argtypes
            kernel32.SetInformationJobObject.restype = BOOL
            kernel32.AssignProcessToJobObject.argtypes = (HANDLE, HANDLE)
            kernel32.AssignProcessToJobObject.restype = BOOL
            kernel32.IsDebuggerPresent.argtypes = ()
            kernel32.IsDebuggerPresent.restype = BOOL
        except (AttributeError, TypeError, ImportError):
            kernel32 = None
        job = kernel32.CreateJobObjectW(None, None) if kernel32 else None
        job = subprocess.Handle(job) if job else job
        if job:
            from ctypes.wintypes import DWORD, LARGE_INTEGER, ULARGE_INTEGER

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", LARGE_INTEGER),
                    ("PerJobUserTimeLimit", LARGE_INTEGER),
                    ("LimitFlags", DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", DWORD),
                    ("SchedulingClass", DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ULARGE_INTEGER),
                    ("WriteOperationCount", ULARGE_INTEGER),
                    ("OtherOperationCount", ULARGE_INTEGER),
                    ("ReadTransferCount", ULARGE_INTEGER),
                    ("WriteTransferCount", ULARGE_INTEGER),
                    ("OtherTransferCount", ULARGE_INTEGER),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            debug = kernel32.IsDebuggerPresent()

            # Defined in <WinNT.h>; also available here:
            # https://docs.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject
            JobObjectExtendedLimitInformation = 9
            JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
            JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
            buf = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            buf.BasicLimitInformation.LimitFlags = (
                (0 if debug else JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
                | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
                | JOB_OBJECT_LIMIT_BREAKAWAY_OK
            )
            infoclass = JobObjectExtendedLimitInformation
            if not kernel32.SetInformationJobObject(
                job, infoclass, ctypes.byref(buf), ctypes.sizeof(buf)
            ):
                job = None
        win32_AssignProcessToJobObject = (
            kernel32.AssignProcessToJobObject if kernel32 is not None else False
        )
        win32_job = job if job else False
    return bool(win32_job)


def detect_fate_sharing_support_linux():
    global linux_prctl
    if linux_prctl is None and sys.platform.startswith("linux"):
        try:
            from ctypes import CDLL, c_int, c_ulong

            prctl = CDLL(None).prctl
            prctl.restype = c_int
            prctl.argtypes = [c_int, c_ulong, c_ulong, c_ulong, c_ulong]
        except (AttributeError, TypeError):
            prctl = None
        linux_prctl = prctl if prctl else False
    return bool(linux_prctl)


def detect_fate_sharing_support():
    result = None
    if sys.platform == "win32":
        result = detect_fate_sharing_support_win32()
    elif sys.platform.startswith("linux"):
        result = detect_fate_sharing_support_linux()
    return result


def set_kill_on_parent_death_linux():
    """Ensures this process dies if its parent dies (fate-sharing).

    Linux-only. Must be called in preexec_fn (i.e. by the child).
    """
    if detect_fate_sharing_support_linux():
        import signal

        PR_SET_PDEATHSIG = 1
        if linux_prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            import ctypes

            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    else:
        assert False, "PR_SET_PDEATHSIG used despite being unavailable"


def set_kill_child_on_death_win32(child_proc):
    """Ensures the child process dies if this process dies (fate-sharing).

    Windows-only. Must be called by the parent, after spawning the child.

    Args:
        child_proc: The subprocess.Popen or subprocess.Handle object.
    """

    if isinstance(child_proc, subprocess.Popen):
        child_proc = child_proc._handle
    assert isinstance(child_proc, subprocess.Handle)

    if detect_fate_sharing_support_win32():
        if not win32_AssignProcessToJobObject(win32_job, int(child_proc)):
            import ctypes

            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject() failed")
    else:
        assert False, "AssignProcessToJobObject used despite being unavailable"


def set_sigterm_handler(sigterm_handler):
    """Registers a handler for SIGTERM in a platform-compatible manner."""
    if sys.platform == "win32":
        # Note that these signal handlers only work for console applications.
        # TODO(mehrdadn): implement graceful process termination mechanism
        # SIGINT is Ctrl+C, SIGBREAK is Ctrl+Break.
        signal.signal(signal.SIGBREAK, sigterm_handler)
    else:
        signal.signal(signal.SIGTERM, sigterm_handler)


def try_make_directory_shared(directory_path):
    try:
        os.chmod(directory_path, 0o0777)
    except OSError as e:
        # Silently suppress the PermissionError that is thrown by the chmod.
        # This is done because the user attempting to change the permissions
        # on a directory may not own it. The chmod is attempted whether the
        # directory is new or not to avoid race conditions.
        # ray-project/ray/#3591
        if e.errno in [errno.EACCES, errno.EPERM]:
            pass
        else:
            raise


def try_to_create_directory(directory_path):
    """Attempt to create a directory that is globally readable/writable.

    Args:
        directory_path: The path of the directory to create.
    """
    directory_path = os.path.expanduser(directory_path)
    os.makedirs(directory_path, exist_ok=True)
    # Change the log directory permissions so others can use it. This is
    # important when multiple people are using the same machine.
    try_make_directory_shared(directory_path)


def try_to_symlink(symlink_path, target_path):
    """Attempt to create a symlink.

    If the symlink path exists and isn't a symlink, the symlink will not be
    created. If a symlink exists in the path, it will be attempted to be
    removed and replaced.

    Args:
        symlink_path: The path at which to create the symlink.
        target_path: The path the symlink should point to.
    """
    symlink_path = os.path.expanduser(symlink_path)
    target_path = os.path.expanduser(target_path)

    if os.path.exists(symlink_path):
        if os.path.islink(symlink_path):
            # Try to remove existing symlink.
            try:
                os.remove(symlink_path)
            except OSError:
                return
        else:
            # There's an existing non-symlink file, don't overwrite it.
            return

    try:
        os.symlink(target_path, symlink_path)
    except OSError:
        return


def get_user():
    if pwd is None:
        return ""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return ""


def get_function_args(callable):
    all_parameters = frozenset(signature(callable).parameters)
    return list(all_parameters)


def get_conda_bin_executable(executable_name):
    """
    Return path to the specified executable, assumed to be discoverable within
    the 'bin' subdirectory of a conda installation.  Adapted from
    https://github.com/mlflow/mlflow.
    """

    # Use CONDA_EXE as per https://github.com/conda/conda/issues/7126
    if "CONDA_EXE" in os.environ:
        conda_bin_dir = os.path.dirname(os.environ["CONDA_EXE"])
        return os.path.join(conda_bin_dir, executable_name)
    return executable_name


def get_conda_env_dir(env_name):
    """Find and validate the conda directory for a given conda environment.

    For example, given the environment name `tf1`, this function checks
    the existence of the corresponding conda directory, e.g.
    `/Users/scaly/anaconda3/envs/tf1`, and returns it.
    """
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix is None:
        # The caller is neither in a conda env or in (base) env.  This is rare
        # because by default, new terminals start in (base), but we can still
        # support this case.
        conda_exe = os.environ.get("CONDA_EXE")
        if conda_exe is None:
            raise ValueError(
                "Cannot find environment variables set by conda. "
                "Please verify conda is installed."
            )
        # Example: CONDA_EXE=$HOME/anaconda3/bin/python
        # Strip out /bin/python by going up two parent directories.
        conda_prefix = str(Path(conda_exe).parent.parent)

    # There are two cases:
    # 1. We are in a conda (base) env: CONDA_DEFAULT_ENV=base and
    #    CONDA_PREFIX=$HOME/anaconda3
    # 2. We are in a user-created conda env: CONDA_DEFAULT_ENV=$env_name and
    #    CONDA_PREFIX=$HOME/anaconda3/envs/$current_env_name
    if os.environ.get("CONDA_DEFAULT_ENV") == "base":
        # Caller's curent environment is (base).
        # Not recommended by conda, but we can still support it.
        if env_name == "base":
            # Desired environment is (base), located at e.g. $HOME/anaconda3
            env_dir = conda_prefix
        else:
            # Desired environment is user-created, e.g.
            # $HOME/anaconda3/envs/$env_name
            env_dir = os.path.join(conda_prefix, "envs", env_name)
    else:
        # Now `conda_prefix` should be something like
        # $HOME/anaconda3/envs/$current_env_name
        # We want to replace the last component with the desired env name.
        conda_envs_dir = os.path.split(conda_prefix)[0]
        env_dir = os.path.join(conda_envs_dir, env_name)
    if not os.path.isdir(env_dir):
        raise ValueError(
            "conda env "
            + env_name
            + " not found in conda envs directory. Run `conda env list` to "
            + "verify the name is correct."
        )
    return env_dir


def get_call_location(back: int = 1):
    """
    Get the location (filename and line number) of a function caller, `back`
    frames up the stack.

    Args:
        back: The number of frames to go up the stack, not including this
            function.
    """
    stack = inspect.stack()
    try:
        frame = stack[back + 1]
        return f"{frame.filename}:{frame.lineno}"
    except IndexError:
        return "UNKNOWN"


def get_ray_doc_version():
    """Get the docs.ray.io version corresponding to the ray.__version__."""
    # The ray.__version__ can be official Ray release (such as 1.12.0), or
    # dev (3.0.0dev0) or release candidate (2.0.0rc0). For the later we map
    # to the master doc version at docs.ray.io.
    if re.match(r"^\d+\.\d+\.\d+$", ray.__version__) is None:
        return "master"
    # For the former (official Ray release), we have corresponding doc version
    # released as well.
    return f"releases-{ray.__version__}"


# Used to only print a deprecation warning once for a given function if we
# don't wish to spam the caller.
_PRINTED_WARNING = set()


# The following is inspired by
# https://github.com/tensorflow/tensorflow/blob/dec8e0b11f4f87693b67e125e67dfbc68d26c205/tensorflow/python/util/deprecation.py#L274-L329
def deprecated(
    instructions: Optional[str] = None,
    removal_release: Optional[str] = None,
    removal_date: Optional[str] = None,
    warn_once: bool = True,
    stacklevel=2,
):
    """
    Creates a decorator for marking functions as deprecated. The decorator
    will log a deprecation warning on the first (or all, see `warn_once` arg)
    invocations, and will otherwise leave the wrapped function unchanged.

    Args:
        instructions: Instructions for the caller to update their code.
        removal_release: The release in which this deprecated function
            will be removed. Only one of removal_release and removal_date
            should be specified. If neither is specfieid, we'll warning that
            the function will be removed "in a future release".
        removal_date: The date on which this deprecated function will be
            removed. Only one of removal_release and removal_date should be
            specified. If neither is specfieid, we'll warning that
            the function will be removed "in a future release".
        warn_once: If true, the deprecation warning will only be logged
            on the first invocation. Otherwise, the deprecation warning will
            be logged on every invocation. Defaults to True.
        stacklevel: adjust the warnings stacklevel to trace the source call

    Returns:
        A decorator to be used for wrapping deprecated functions.
    """
    if removal_release is not None and removal_date is not None:
        raise ValueError(
            "Only one of removal_release and removal_date should be specified."
        )

    def deprecated_wrapper(func):
        @functools.wraps(func)
        def new_func(*args, **kwargs):
            global _PRINTED_WARNING
            if func not in _PRINTED_WARNING:
                if warn_once:
                    _PRINTED_WARNING.add(func)
                msg = (
                    "From {}: {} (from {}) is deprecated and will ".format(
                        get_call_location(), func.__name__, func.__module__
                    )
                    + "be removed "
                    + (
                        f"in version {removal_release}."
                        if removal_release is not None
                        else f"after {removal_date}"
                        if removal_date is not None
                        else "in a future version"
                    )
                    + (f" {instructions}" if instructions is not None else "")
                )
                warnings.warn(msg, stacklevel=stacklevel)
            return func(*args, **kwargs)

        return new_func

    return deprecated_wrapper


def import_attr(full_path: str, *, reload_module: bool = False):
    """Given a full import path to a module attr, return the imported attr.

    If `reload_module` is set, the module will be reloaded using `importlib.reload`.

    For example, the following are equivalent:
        MyClass = import_attr("module.submodule:MyClass")
        MyClass = import_attr("module.submodule.MyClass")
        from module.submodule import MyClass

    Returns:
        Imported attr
    """
    if full_path is None:
        raise TypeError("import path cannot be None")

    if ":" in full_path:
        if full_path.count(":") > 1:
            raise ValueError(
                f'Got invalid import path "{full_path}". An '
                "import path may have at most one colon."
            )
        module_name, attr_name = full_path.split(":")
    else:
        last_period_idx = full_path.rfind(".")
        module_name = full_path[:last_period_idx]
        attr_name = full_path[last_period_idx + 1 :]

    module = importlib.import_module(module_name)
    if reload_module:
        importlib.reload(module)
    return getattr(module, attr_name)


def get_wheel_filename(
    sys_platform: str = sys.platform,
    ray_version: str = ray.__version__,
    py_version: Tuple[int, int] = (sys.version_info.major, sys.version_info.minor),
    architecture: Optional[str] = None,
) -> str:
    """Returns the filename used for the nightly Ray wheel.

    Args:
        sys_platform: The platform as returned by sys.platform. Examples:
            "darwin", "linux", "win32"
        ray_version: The Ray version as returned by ray.__version__ or
            `ray --version`.  Examples: "3.0.0.dev0"
        py_version: The Python version as returned by sys.version_info. A
            tuple of (major, minor). Examples: (3, 8)
        architecture: Architecture, e.g. ``x86_64`` or ``aarch64``. If None, will
            be determined by calling ``platform.processor()``.

    Returns:
        The wheel file name.  Examples:
            ray-3.0.0.dev0-cp38-cp38-manylinux2014_x86_64.whl
    """
    assert py_version in ray_constants.RUNTIME_ENV_CONDA_PY_VERSIONS, py_version

    py_version_str = "".join(map(str, py_version))

    architecture = architecture or platform.processor()

    if py_version_str in ["311", "310", "39", "38"] and architecture == "arm64":
        darwin_os_string = "macosx_11_0_arm64"
    else:
        darwin_os_string = "macosx_10_15_x86_64"

    if architecture == "aarch64":
        linux_os_string = "manylinux2014_aarch64"
    else:
        linux_os_string = "manylinux2014_x86_64"

    os_strings = {
        "darwin": darwin_os_string,
        "linux": linux_os_string,
        "win32": "win_amd64",
    }

    assert sys_platform in os_strings, sys_platform

    wheel_filename = (
        f"ray-{ray_version}-cp{py_version_str}-"
        f"cp{py_version_str}{'m' if py_version_str in ['37'] else ''}"
        f"-{os_strings[sys_platform]}.whl"
    )

    return wheel_filename


def get_master_wheel_url(
    ray_commit: str = ray.__commit__,
    sys_platform: str = sys.platform,
    ray_version: str = ray.__version__,
    py_version: Tuple[int, int] = sys.version_info[:2],
) -> str:
    """Return the URL for the wheel from a specific commit."""
    filename = get_wheel_filename(
        sys_platform=sys_platform, ray_version=ray_version, py_version=py_version
    )
    return (
        f"https://s3-us-west-2.amazonaws.com/ray-wheels/master/"
        f"{ray_commit}/{filename}"
    )


def get_release_wheel_url(
    ray_commit: str = ray.__commit__,
    sys_platform: str = sys.platform,
    ray_version: str = ray.__version__,
    py_version: Tuple[int, int] = sys.version_info[:2],
) -> str:
    """Return the URL for the wheel for a specific release."""
    filename = get_wheel_filename(
        sys_platform=sys_platform, ray_version=ray_version, py_version=py_version
    )
    return (
        f"https://ray-wheels.s3-us-west-2.amazonaws.com/releases/"
        f"{ray_version}/{ray_commit}/{filename}"
    )
    # e.g. https://ray-wheels.s3-us-west-2.amazonaws.com/releases/1.4.0rc1/e7c7
    # f6371a69eb727fa469e4cd6f4fbefd143b4c/ray-1.4.0rc1-cp36-cp36m-manylinux201
    # 4_x86_64.whl


def validate_namespace(namespace: str):
    if not isinstance(namespace, str):
        raise TypeError("namespace must be None or a string.")
    elif namespace == "":
        raise ValueError(
            '"" is not a valid namespace. ' "Pass None to not specify a namespace."
        )


def init_grpc_channel(
    address: str,
    options: Optional[Sequence[Tuple[str, Any]]] = None,
    asynchronous: bool = False,
):
    import grpc

    try:
        from grpc import aio as aiogrpc
    except ImportError:
        from grpc.experimental import aio as aiogrpc

    from ray._private.tls_utils import load_certs_from_env

    grpc_module = aiogrpc if asynchronous else grpc

    options = options or []
    options_dict = dict(options)
    options_dict["grpc.keepalive_time_ms"] = options_dict.get(
        "grpc.keepalive_time_ms", ray._config.grpc_client_keepalive_time_ms()
    )
    options_dict["grpc.keepalive_timeout_ms"] = options_dict.get(
        "grpc.keepalive_timeout_ms", ray._config.grpc_client_keepalive_timeout_ms()
    )
    options = options_dict.items()

    if os.environ.get("RAY_USE_TLS", "0").lower() in ("1", "true"):
        server_cert_chain, private_key, ca_cert = load_certs_from_env()
        credentials = grpc.ssl_channel_credentials(
            certificate_chain=server_cert_chain,
            private_key=private_key,
            root_certificates=ca_cert,
        )
        channel = grpc_module.secure_channel(address, credentials, options=options)
    else:
        channel = grpc_module.insecure_channel(address, options=options)

    return channel


def check_dashboard_dependencies_installed() -> bool:
    """Returns True if Ray Dashboard dependencies are installed.

    Checks to see if we should start the dashboard agent or not based on the
    Ray installation version the user has installed (ray vs. ray[default]).
    Unfortunately there doesn't seem to be a cleaner way to detect this other
    than just blindly importing the relevant packages.

    """
    try:
        import ray.dashboard.optional_deps  # noqa: F401

        return True
    except ImportError:
        return False


def check_ray_client_dependencies_installed() -> bool:
    """Returns True if Ray Client dependencies are installed.

    See documents for check_dashboard_dependencies_installed.
    """
    try:
        import grpc  # noqa: F401

        return True
    except ImportError:
        return False


connect_error = (
    "Unable to connect to GCS (ray head) at {}. "
    "Check that (1) Ray with matching version started "
    "successfully at the specified address, (2) this "
    "node can reach the specified address, and (3) there is "
    "no firewall setting preventing access."
)


def internal_kv_list_with_retry(gcs_client, prefix, namespace, num_retries=20):
    result = None
    if isinstance(prefix, str):
        prefix = prefix.encode()
    if isinstance(namespace, str):
        namespace = namespace.encode()
    for _ in range(num_retries):
        try:
            result = gcs_client.internal_kv_keys(prefix, namespace)
        except Exception as e:
            if isinstance(e, ray.exceptions.RpcError) and e.rpc_code in (
                ray._raylet.GRPC_STATUS_CODE_UNAVAILABLE,
                ray._raylet.GRPC_STATUS_CODE_UNKNOWN,
            ):
                logger.warning(connect_error.format(gcs_client.address))
            else:
                logger.exception("Internal KV List failed")
            result = None

        if result is not None:
            break
        else:
            logger.debug(f"Fetched {prefix}=None from KV. Retrying.")
            time.sleep(2)
    if result is None:
        raise ConnectionError(
            f"Could not list '{prefix}' from GCS. Did GCS start successfully?"
        )
    return result


def internal_kv_get_with_retry(gcs_client, key, namespace, num_retries=20):
    result = None
    if isinstance(key, str):
        key = key.encode()
    for _ in range(num_retries):
        try:
            result = gcs_client.internal_kv_get(key, namespace)
        except Exception as e:
            if isinstance(e, ray.exceptions.RpcError) and e.rpc_code in (
                ray._raylet.GRPC_STATUS_CODE_UNAVAILABLE,
                ray._raylet.GRPC_STATUS_CODE_UNKNOWN,
            ):
                logger.warning(connect_error.format(gcs_client.address))
            else:
                logger.exception("Internal KV Get failed")
            result = None

        if result is not None:
            break
        else:
            logger.debug(f"Fetched {key}=None from KV. Retrying.")
            time.sleep(2)
    if not result:
        raise ConnectionError(
            f"Could not read '{key.decode()}' from GCS. Did GCS start successfully?"
        )
    return result


def parse_resources_json(
    resources: str, cli_logger, cf, command_arg="--resources"
) -> Dict[str, float]:
    try:
        resources = json.loads(resources)
        if not isinstance(resources, dict):
            raise ValueError("The format after deserialization is not a dict")
    except Exception as e:
        cli_logger.error(
            "`{}` is not a valid JSON string, detail error:{}",
            cf.bold(f"{command_arg}={resources}"),
            str(e),
        )
        cli_logger.abort(
            "Valid values look like this: `{}`",
            cf.bold(
                f'{command_arg}=\'{{"CustomResource3": 1, "CustomResource2": 2}}\''
            ),
        )
    return resources


def parse_metadata_json(
    metadata: str, cli_logger, cf, command_arg="--metadata-json"
) -> Dict[str, str]:
    try:
        metadata = json.loads(metadata)
        if not isinstance(metadata, dict):
            raise ValueError("The format after deserialization is not a dict")
    except Exception as e:
        cli_logger.error(
            "`{}` is not a valid JSON string, detail error:{}",
            cf.bold(f"{command_arg}={metadata}"),
            str(e),
        )
        cli_logger.abort(
            "Valid values look like this: `{}`",
            cf.bold(f'{command_arg}=\'{{"key1": "value1", "key2": "value2"}}\''),
        )
    return metadata


def internal_kv_put_with_retry(gcs_client, key, value, namespace, num_retries=20):
    if isinstance(key, str):
        key = key.encode()
    if isinstance(value, str):
        value = value.encode()
    if isinstance(namespace, str):
        namespace = namespace.encode()
    error = None
    for _ in range(num_retries):
        try:
            return gcs_client.internal_kv_put(
                key, value, overwrite=True, namespace=namespace
            )
        except ray.exceptions.RpcError as e:
            if e.rpc_code in (
                ray._raylet.GRPC_STATUS_CODE_UNAVAILABLE,
                ray._raylet.GRPC_STATUS_CODE_UNKNOWN,
            ):
                logger.warning(connect_error.format(gcs_client.address))
            else:
                logger.exception("Internal KV Put failed")
            time.sleep(2)
            error = e
    # Reraise the last error.
    raise error


def compute_version_info():
    """Compute the versions of Python, and Ray.

    Returns:
        A tuple containing the version information.
    """
    ray_version = ray.__version__
    python_version = ".".join(map(str, sys.version_info[:3]))
    return ray_version, python_version


def get_directory_size_bytes(path: Union[str, Path] = ".") -> int:
    """Get the total size of a directory in bytes, including subdirectories."""
    total_size_bytes = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            # skip if it is a symbolic link or a .pyc file
            if not os.path.islink(fp) and not f.endswith(".pyc"):
                total_size_bytes += os.path.getsize(fp)

    return total_size_bytes


def check_version_info(cluster_metadata):
    """Check if the Python and Ray versions stored in GCS matches this process.
    Args:
        cluster_metadata: Ray cluster metadata from GCS.

    Raises:
        Exception: An exception is raised if there is a version mismatch.
    """
    cluster_version_info = (
        cluster_metadata["ray_version"],
        cluster_metadata["python_version"],
    )
    version_info = compute_version_info()
    if version_info != cluster_version_info:
        node_ip_address = ray._private.services.get_node_ip_address()
        error_message = (
            "Version mismatch: The cluster was started with:\n"
            "    Ray: " + cluster_version_info[0] + "\n"
            "    Python: " + cluster_version_info[1] + "\n"
            "This process on node " + node_ip_address + " was started with:" + "\n"
            "    Ray: " + version_info[0] + "\n"
            "    Python: " + version_info[1] + "\n"
        )
        raise RuntimeError(error_message)


def get_runtime_env_info(
    runtime_env: "RuntimeEnv",
    *,
    is_job_runtime_env: bool = False,
    serialize: bool = False,
):
    """Create runtime env info from runtime env.

    In the user interface, the argument `runtime_env` contains some fields
    which not contained in `ProtoRuntimeEnv` but in `ProtoRuntimeEnvInfo`,
    such as `eager_install`. This function will extract those fields from
    `RuntimeEnv` and create a new `ProtoRuntimeEnvInfo`, and serialize it.
    """
    from ray.runtime_env import RuntimeEnvConfig

    proto_runtime_env_info = ProtoRuntimeEnvInfo()

    if runtime_env.working_dir_uri():
        proto_runtime_env_info.uris.working_dir_uri = runtime_env.working_dir_uri()
    if len(runtime_env.py_modules_uris()) > 0:
        proto_runtime_env_info.uris.py_modules_uris[:] = runtime_env.py_modules_uris()

    # TODO(Catch-Bull): overload `__setitem__` for `RuntimeEnv`, change the
    # runtime_env of all internal code from dict to RuntimeEnv.

    runtime_env_config = runtime_env.get("config")
    if runtime_env_config is None:
        runtime_env_config = RuntimeEnvConfig.default_config()
    else:
        runtime_env_config = RuntimeEnvConfig.parse_and_validate_runtime_env_config(
            runtime_env_config
        )

    proto_runtime_env_info.runtime_env_config.CopyFrom(
        runtime_env_config.build_proto_runtime_env_config()
    )

    # Normally, `RuntimeEnv` should guarantee the accuracy of field eager_install,
    # but so far, the internal code has not completely prohibited direct
    # modification of fields in RuntimeEnv, so we should check it for insurance.
    eager_install = (
        runtime_env_config.get("eager_install")
        if runtime_env_config is not None
        else None
    )
    if is_job_runtime_env or eager_install is not None:
        if eager_install is None:
            eager_install = True
        elif not isinstance(eager_install, bool):
            raise TypeError(
                f"eager_install must be a boolean. got {type(eager_install)}"
            )
        proto_runtime_env_info.runtime_env_config.eager_install = eager_install

    proto_runtime_env_info.serialized_runtime_env = runtime_env.serialize()

    if not serialize:
        return proto_runtime_env_info

    return json_format.MessageToJson(proto_runtime_env_info)


def parse_runtime_env(runtime_env: Optional[Union[Dict, "RuntimeEnv"]]):
    from ray.runtime_env import RuntimeEnv

    # Parse local pip/conda config files here. If we instead did it in
    # .remote(), it would get run in the Ray Client server, which runs on
    # a remote node where the files aren't available.
    if runtime_env:
        if isinstance(runtime_env, dict):
            return RuntimeEnv(**(runtime_env or {}))
        raise TypeError(
            "runtime_env must be dict or RuntimeEnv, ",
            f"but got: {type(runtime_env)}",
        )
    else:
        # Keep the new_runtime_env as None.  In .remote(), we need to know
        # if runtime_env is None to know whether or not to fall back to the
        # runtime_env specified in the @ray.remote decorator.
        return None


def split_address(address: str) -> Tuple[str, str]:
    """Splits address into a module string (scheme) and an inner_address.

    We use a custom splitting function instead of urllib because
    PEP allows "underscores" in a module names, while URL schemes do not
    allow them.

    Args:
        address: The address to split.

    Returns:
        A tuple of (scheme, inner_address).

    Raises:
        ValueError: If the address does not contain '://'.

    Examples:
        >>> split_address("ray://my_cluster")
        ('ray', 'my_cluster')
    """
    if "://" not in address:
        raise ValueError("Address must contain '://'")

    module_string, inner_address = address.split("://", maxsplit=1)
    return (module_string, inner_address)


def get_or_create_event_loop() -> asyncio.BaseEventLoop:
    """Get a running async event loop if one exists, otherwise create one.

    This function serves as a proxy for the deprecating get_event_loop().
    It tries to get the running loop first, and if no running loop
    could be retrieved:
    - For python version <3.10: it falls back to the get_event_loop
        call.
    - For python version >= 3.10: it uses the same python implementation
        of _get_event_loop() at asyncio/events.py.

    Ideally, one should use high level APIs like asyncio.run() with python
    version >= 3.7, if not possible, one should create and manage the event
    loops explicitly.
    """
    vers_info = sys.version_info
    if vers_info.major >= 3 and vers_info.minor >= 10:
        # This follows the implementation of the deprecating `get_event_loop`
        # in python3.10's asyncio. See python3.10/asyncio/events.py
        # _get_event_loop()
        loop = None
        try:
            loop = asyncio.get_running_loop()
            assert loop is not None
            return loop
        except RuntimeError as e:
            # No running loop, relying on the error message as for now to
            # differentiate runtime errors.
            assert "no running event loop" in str(e)
            return asyncio.get_event_loop_policy().get_event_loop()

    return asyncio.get_event_loop()


def get_entrypoint_name():
    """Get the entrypoint of the current script."""
    prefix = ""
    try:
        curr = psutil.Process()
        # Prepend `interactive_shell` for interactive shell scripts.
        # https://stackoverflow.com/questions/2356399/tell-if-python-is-in-interactive-mode # noqa
        if hasattr(sys, "ps1"):
            prefix = "(interactive_shell) "

        return prefix + list2cmdline(curr.cmdline())
    except Exception:
        return "unknown"


def _add_url_query_params(url: str, params: Dict[str, str]) -> str:
    """Add params to the provided url as query parameters.

    If url already contains query parameters, they will be merged with params, with the
    existing query parameters overriding any in params with the same parameter name.

    Args:
        url: The URL to add query parameters to.
        params: The query parameters to add.

    Returns:
        URL with params added as query parameters.
    """
    # Unquote URL first so we don't lose existing args.
    url = unquote(url)
    # Parse URL.
    parsed_url = urlparse(url)
    # Merge URL query string arguments dict with new params.
    base_params = params
    params = dict(parse_qsl(parsed_url.query))
    base_params.update(params)
    # bool and dict values should be converted to json-friendly values.
    base_params.update(
        {
            k: json.dumps(v)
            for k, v in base_params.items()
            if isinstance(v, (bool, dict))
        }
    )

    # Convert URL arguments to proper query string.
    encoded_params = urlencode(base_params, doseq=True)
    # Replace query string in parsed URL with updated query string.
    parsed_url = parsed_url._replace(query=encoded_params)
    # Convert back to URL.
    return urlunparse(parsed_url)


def _add_creatable_buckets_param_if_s3_uri(uri: str) -> str:
    """If the provided URI is an S3 URL, add allow_bucket_creation=true as a query
    parameter. For pyarrow >= 9.0.0, this is required in order to allow
    ``S3FileSystem.create_dir()`` to create S3 buckets.

    If the provided URI is not an S3 URL or if pyarrow < 9.0.0 is installed, we return
    the URI unchanged.

    Args:
        uri: The URI that we'll add the query parameter to, if it's an S3 URL.

    Returns:
        A URI with the added allow_bucket_creation=true query parameter, if the provided
        URI is an S3 URL; uri will be returned unchanged otherwise.
    """
    from pkg_resources._vendor.packaging.version import parse as parse_version

    pyarrow_version = _get_pyarrow_version()
    if pyarrow_version is not None:
        pyarrow_version = parse_version(pyarrow_version)
    if pyarrow_version is not None and pyarrow_version < parse_version("9.0.0"):
        # This bucket creation query parameter is not required for pyarrow < 9.0.0.
        return uri
    parsed_uri = urlparse(uri)
    if parsed_uri.scheme == "s3":
        uri = _add_url_query_params(uri, {"allow_bucket_creation": True})
    return uri


def _get_pyarrow_version() -> Optional[str]:
    """Get the version of the installed pyarrow package, returned as a tuple of ints.
    Returns None if the package is not found.
    """
    global _PYARROW_VERSION
    if _PYARROW_VERSION is None:
        try:
            import pyarrow
        except ModuleNotFoundError:
            # pyarrow not installed, short-circuit.
            pass
        else:
            if hasattr(pyarrow, "__version__"):
                _PYARROW_VERSION = pyarrow.__version__
    return _PYARROW_VERSION


class DeferSigint(contextlib.AbstractContextManager):
    """Context manager that defers SIGINT signals until the the context is left."""

    # This is used by Ray's task cancellation to defer cancellation interrupts during
    # problematic areas, e.g. task argument deserialization.
    def __init__(self):
        # Whether the task has been cancelled while in the context.
        self.task_cancelled = False
        # The original SIGINT handler.
        self.orig_sigint_handler = None
        # The original signal method.
        self.orig_signal = None

    @classmethod
    def create_if_main_thread(cls) -> contextlib.AbstractContextManager:
        """Creates a DeferSigint context manager if running on the main thread,
        returns a no-op context manager otherwise.
        """
        if threading.current_thread() == threading.main_thread():
            return cls()
        else:
            return contextlib.nullcontext()

    def _set_task_cancelled(self, signum, frame):
        """SIGINT handler that defers the signal."""
        self.task_cancelled = True

    def _signal_monkey_patch(self, signum, handler):
        """Monkey patch for signal.signal that raises an error if a SIGINT handler is
        registered within the DeferSigint context.
        """
        # Only raise an error if setting a SIGINT handler in the main thread; if setting
        # a handler in a non-main thread, signal.signal will raise an error anyway
        # indicating that Python does not allow that.
        if (
            threading.current_thread() == threading.main_thread()
            and signum == signal.SIGINT
        ):
            raise ValueError(
                "Can't set signal handler for SIGINT while SIGINT is being deferred "
                "within a DeferSigint context."
            )
        return self.orig_signal(signum, handler)

    def __enter__(self):
        # Save original SIGINT handler for later restoration.
        self.orig_sigint_handler = signal.getsignal(signal.SIGINT)
        # Set SIGINT signal handler that defers the signal.
        signal.signal(signal.SIGINT, self._set_task_cancelled)
        # Monkey patch signal.signal to raise an error if a SIGINT handler is registered
        # within the context.
        self.orig_signal = signal.signal
        signal.signal = self._signal_monkey_patch
        return self

    def __exit__(self, exc_type, exc, exc_tb):
        assert self.orig_sigint_handler is not None
        assert self.orig_signal is not None
        # Restore original signal.signal function.
        signal.signal = self.orig_signal
        # Restore original SIGINT handler.
        signal.signal(signal.SIGINT, self.orig_sigint_handler)
        if exc_type is None and self.task_cancelled:
            # No exception raised in context but task has been cancelled, so we raise
            # KeyboardInterrupt to go through the task cancellation path.
            raise KeyboardInterrupt
        else:
            # If exception was raised in context, returning False will cause it to be
            # reraised.
            return False


background_tasks = set()


def run_background_task(coroutine: Coroutine) -> asyncio.Task:
    """Schedule a task reliably to the event loop.

    This API is used when you don't want to cache the reference of `asyncio.Task`.
    For example,

    ```
    get_event_loop().create_task(coroutine(*args))
    ```

    The above code doesn't guarantee to schedule the coroutine to the event loops

    When using create_task in a  "fire and forget" way, we should keep the references
    alive for the reliable execution. This API is used to fire and forget
    asynchronous execution.

    https://docs.python.org/3/library/asyncio-task.html#creating-tasks
    """
    task = get_or_create_event_loop().create_task(coroutine)
    # Add task to the set. This creates a strong reference.
    background_tasks.add(task)

    # To prevent keeping references to finished tasks forever,
    # make each task remove its own reference from the set after
    # completion:
    task.add_done_callback(background_tasks.discard)
    return task


def try_import_each_module(module_names_to_import: List[str]) -> None:
    """
    Make a best-effort attempt to import each named Python module.
    This is used by the Python default_worker.py to preload modules.
    """
    for module_to_preload in module_names_to_import:
        try:
            importlib.import_module(module_to_preload)
        except ImportError:
            logger.exception(f'Failed to preload the module "{module_to_preload}"')


def update_envs(env_vars: Dict[str, str]):
    """
    When updating the environment variable, if there is ${X},
    it will be replaced with the current environment variable.
    """
    if not env_vars:
        return

    for key, value in env_vars.items():
        expanded = os.path.expandvars(value)
        # Replace non-existant env vars with an empty string.
        result = re.sub(r"\$\{[A-Z0-9_]+\}", "", expanded)
        os.environ[key] = result


def parse_node_labels_json(
    labels_json: str, cli_logger, cf, command_arg="--labels"
) -> Dict[str, str]:
    try:
        labels = json.loads(labels_json)
        if not isinstance(labels, dict):
            raise ValueError(
                "The format after deserialization is not a key-value pair map"
            )
        for key, value in labels.items():
            if not isinstance(key, str):
                raise ValueError("The key is not string type.")
            if not isinstance(value, str):
                raise ValueError(f'The value of the "{key}" is not string type')
    except Exception as e:
        cli_logger.abort(
            "`{}` is not a valid JSON string, detail error:{}"
            "Valid values look like this: `{}`",
            cf.bold(f"{command_arg}={labels_json}"),
            str(e),
            cf.bold(f'{command_arg}=\'{{"gpu_type": "A100", "region": "us"}}\''),
        )
    return labels


def validate_node_labels(labels: Dict[str, str]):
    if labels is None:
        return
    for key in labels.keys():
        if key.startswith(ray_constants.RAY_DEFAULT_LABEL_KEYS_PREFIX):
            raise ValueError(
                f"Custom label keys `{key}` cannot start with the prefix "
                f"`{ray_constants.RAY_DEFAULT_LABEL_KEYS_PREFIX}`. "
                f"This is reserved for Ray defined labels."
            )


def pasre_pg_formatted_resources_to_original(
    pg_formatted_resources: Dict[str, float]
) -> Dict[str, float]:
    original_resources = {}

    for key, value in pg_formatted_resources.items():
        result = PLACEMENT_GROUP_WILDCARD_RESOURCE_PATTERN.match(key)
        if result and len(result.groups()) == 2:
            original_resources[result.group(1)] = value
            continue
        result = PLACEMENT_GROUP_INDEXED_BUNDLED_RESOURCE_PATTERN.match(key)
        if result and len(result.groups()) == 3:
            original_resources[result.group(1)] = value
            continue
        original_resources[key] = value

    return original_resources


def load_class(path):
    """Load a class at runtime given a full path.

    Example of the path: mypkg.mysubpkg.myclass
    """
    class_data = path.split(".")
    if len(class_data) < 2:
        raise ValueError("You need to pass a valid path like mymodule.provider_class")
    module_path = ".".join(class_data[:-1])
    class_str = class_data[-1]
    module = importlib.import_module(module_path)
    return getattr(module, class_str)


def validate_actor_state_name(actor_state_name):
    if actor_state_name is None:
        return
    actor_state_names = [
        "DEPENDENCIES_UNREADY",
        "PENDING_CREATION",
        "ALIVE",
        "RESTARTING",
        "DEAD",
    ]
    if actor_state_name not in actor_state_names:
        raise ValueError(
            f'"{actor_state_name}" is not a valid actor state name, '
            'it must be one of the following: "DEPENDENCIES_UNREADY", '
            '"PENDING_CREATION", "ALIVE", "RESTARTING", or "DEAD"'
        )
