import concurrent.futures
import threading
import time

from ournotes.common.secrets import SecretStore


def test_waiting_lane_does_not_block_lock_owner(tmp_path):
    store = SecretStore(tmp_path)
    waiting = threading.Event()

    def contender():
        waiting.set()
        with store.lock('archive', wait=True):
            return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with store.lock('archive'):
            future = pool.submit(contender)
            assert waiting.wait(1)
            time.sleep(.15)
            assert not future.done()
        assert future.result(timeout=2)
