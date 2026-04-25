"""
GPU safety helpers — detect CUDA OOM and fall back to CPU for that call.

Two APIs:
  * is_cuda_oom(exc) -> bool
      Heuristic: True if exception message contains known OOM patterns.

  * @gpu_oom_fallback(cpu_fn)
      Decorator for a GPU-inference function. On OOM, calls cpu_fn(*args, **kwargs).

  * run_with_gpu_oom_fallback(gpu_fn, cpu_fn, *args, **kwargs)
      Functional API — same behavior without a decorator.

Notes:
  * CUDA OOM surfaces differently across torch/onnxruntime. We catch both.
  * After OOM, torch.cuda.empty_cache() is called to release reserved memory.
  * Log a WARNING whenever fallback triggers — this is worth seeing.

Example usage (do not apply to enhance.py in this agent — that's integration work):

    from utils.gpu_safe import run_with_gpu_oom_fallback

    def _enhance_on_gpu(img, upscale, weight):
        restorer = _get_restorer_gpu(upscale)
        return restorer.enhance(img, ...)

    def _enhance_on_cpu(img, upscale, weight):
        restorer = _get_restorer_cpu(upscale)
        return restorer.enhance(img, ...)

    output = run_with_gpu_oom_fallback(_enhance_on_gpu, _enhance_on_cpu, img, upscale, weight)
"""
import logging
from typing import Callable, TypeVar, ParamSpec

logger = logging.getLogger("mantra.agent.gpu_safe")

P = ParamSpec("P")
R = TypeVar("R")


def is_cuda_oom(exc: BaseException) -> bool:
    """
    Heuristic OOM detector. Covers PyTorch and ONNX Runtime flavors.
    """
    msg = str(exc).lower()
    markers = (
        "cuda out of memory",
        "out of memory",
        "cudnn_status_not_supported",  # sometimes masks OOM
        "failed to allocate",
        "runtimeerror: cuda error: out of memory",
        "cuda error: out of memory",
        "onnxruntime::cudaexecutionprovider",  # ORT CUDA provider failure
    )
    return any(m in msg for m in markers)


def _empty_cuda_cache() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def run_with_gpu_oom_fallback(
    gpu_fn: Callable[..., R],
    cpu_fn: Callable[..., R],
    *args,
    **kwargs,
) -> R:
    """Run gpu_fn; on CUDA OOM, call cpu_fn as fallback."""
    try:
        return gpu_fn(*args, **kwargs)
    except Exception as e:
        if is_cuda_oom(e):
            logger.warning("CUDA OOM detected (%s) — falling back to CPU path", e.__class__.__name__)
            _empty_cuda_cache()
            return cpu_fn(*args, **kwargs)
        raise


def gpu_oom_fallback(cpu_fn: Callable[P, R]) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator form:
        @gpu_oom_fallback(my_cpu_function)
        def my_gpu_function(...): ...
    """
    def decorator(gpu_fn: Callable[P, R]) -> Callable[P, R]:
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            return run_with_gpu_oom_fallback(gpu_fn, cpu_fn, *args, **kwargs)
        wrapped.__wrapped__ = gpu_fn  # preserve for introspection
        wrapped.__name__ = gpu_fn.__name__
        return wrapped
    return decorator
