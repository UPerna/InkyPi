"""Keep a stalled vendor call from blocking the refresh queue or overlapping another write."""
from concurrent.futures import Future, wait
from threading import Lock, Thread


class PanelUnavailable(RuntimeError):
    """A user-visible panel state with no underlying driver details."""


class PanelIO:
    def __init__(self, timeout=180):
        self.timeout = timeout
        self._lock = Lock()
        self._active = None
        self._uncertain = False

    def write(self, driver, frame, settings, options, force=False):
        with self._lock:
            if self._active is not None:
                raise PanelUnavailable("The previous panel operation is still running.")
            if self._uncertain and not force:
                raise PanelUnavailable("Panel state is uncertain. Use Force panel refresh to retry.")
            result = Future()
            self._active = result
            self._uncertain = False
            def transfer():
                try:
                    driver.display_image(frame, settings, **options)
                except Exception as error:
                    result.set_exception(error)
                except BaseException:
                    result.set_exception(RuntimeError("The panel driver exited unexpectedly"))
                else:
                    result.set_result(None)

            # Only the scheduler calls write. A late return can never publish a frame.
            thread = Thread(target=transfer, daemon=True, name="panel-io")
            try:
                thread.start()
            except RuntimeError:
                self._active = None
                raise

        done, _ = wait([result], timeout=self.timeout)
        if not done:
            with self._lock:
                self._uncertain = True
            result.add_done_callback(self._release)
            raise PanelUnavailable("Panel timed out. Check the display before forcing a retry.")
        try:
            result.result()
        finally:
            self._release(result)

    def _release(self, result):
        with self._lock:
            if self._active is result:
                self._active = None
