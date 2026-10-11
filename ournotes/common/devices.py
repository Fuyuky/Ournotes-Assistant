"""Cross-process device leases shared by scheduler, rebind and diagnostics."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path

from ournotes.common.secrets import SecretStore
from ournotes.platforms.locking import busy, try_lock, unlock

DEVICE_LEASE_PROTOCOL = 1


def lock_held(root, name):
    """Probe an existing lock without creating, truncating or deleting its file."""
    path = Path(root) / 'state' / f'{name}.lock'
    if not path.exists():
        return False
    try:
        with path.open('r+b') as stream:
            try_lock(stream)
            unlock(stream)
        return False
    except OSError as error:
        if busy(error):
            return True
        # An unreadable lease cannot safely be treated as an available device.
        raise


@contextmanager
def instance_leases(root, instances):
    instances = list(instances)
    if any(type(instance) is not int or instance < 0 for instance in instances):
        raise ValueError('实例编号必须为非负整数。')
    locks = SecretStore(Path(root) / 'state')
    with ExitStack() as stack:
        for instance in sorted(set(instances)):
            try:
                stack.enter_context(locks.lock(f'device-{instance}'))
            except RuntimeError:
                raise ValueError(f'MuMu 实例 {instance} 正由其他任务占用；未启动或恢复设备。') from None
        yield


def require_instances_available(root, instances):
    """Fast UI preflight only; execution must still acquire instance_leases."""
    for instance in instances:
        if lock_held(root, f'device-{instance}'):
            raise ValueError(f'MuMu 实例 {instance} 正由其他任务占用，请选择空闲实例。')


@contextmanager
def rebind_lease(root, instance):
    # Only one rebind executor may control devices in a data root.
    with SecretStore(Path(root) / 'state').lock('email-rebind'), instance_leases(root, [instance]):
        yield
