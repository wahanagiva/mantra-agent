"""
Retry helper with exponential backoff.

Two APIs:
  * retry(fn, *args, max_attempts=3, base_delay=1.0, backoff=2.0, retry_on=(Exception,))
      Run fn(*args). If it raises a retriable exception, wait base_delay * backoff**n
      and retry up to max_attempts times. Returns the first successful result or raises
      the last exception.

  * @retriable(max_attempts=3, base_delay=1.0, backoff=2.0, retry_on=(Exception,))
      Decorator form.
"""
import logging
import time
from functools import wraps
from typing import Callable, Tuple, Type

logger = logging.getLogger("mantra.agent.retry")


def retry(
    fn: Callable,
    *args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    **kwargs,
):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retry_on as e:
            last_exc = e
            if attempt == max_attempts:
                logger.error("Retry exhausted after %d attempts: %s", max_attempts, e)
                raise
            delay = base_delay * (backoff ** (attempt - 1))
            logger.warning(
                "Attempt %d/%d failed (%s: %s) — retrying in %.1fs",
                attempt, max_attempts, e.__class__.__name__, e, delay,
            )
            time.sleep(delay)
    raise last_exc  # pragma: no cover  (unreachable)


def retriable(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
):
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return retry(fn, *args,
                         max_attempts=max_attempts, base_delay=base_delay,
                         backoff=backoff, retry_on=retry_on, **kwargs)
        return wrapped
    return decorator
