"""
Compatibility shims required BEFORE importing gfpgan/basicsr.

Phase 1 (remove-bg only) does NOT need this, but we keep the helper here
so Phase 2 (enhance-face) can call apply_all_patches() on startup before
any GFPGAN import.

Mirrors VPS gpu_api_server.py lines 36-43 exactly.
"""
import sys
import types


def patch_torchvision_functional_tensor() -> bool:
    """
    basicsr 1.4.2 imports `torchvision.transforms.functional_tensor` which was
    removed in torchvision 0.17+. Without this shim, GFPGAN imports fail.

    Returns True if patch applied, False if torchvision not installed (skip).
    Idempotent — safe to call multiple times.
    """
    if "torchvision.transforms.functional_tensor" in sys.modules:
        return True
    try:
        import torchvision.transforms.functional as F
    except ImportError:
        return False

    shim = types.ModuleType("torchvision.transforms.functional_tensor")
    # basicsr only needs rgb_to_grayscale
    shim.rgb_to_grayscale = F.rgb_to_grayscale
    sys.modules["torchvision.transforms.functional_tensor"] = shim
    return True


def apply_all_patches() -> dict:
    """Apply all known compat shims. Call ONCE on agent startup, before handler imports."""
    return {
        "torchvision_functional_tensor": patch_torchvision_functional_tensor(),
    }
