# Bundled Visual C++ Runtime DLLs

These are Microsoft Visual C++ Redistributable runtime DLLs (2015-2022 / VS 17.x),
bundled here so the bat installer can copy them next to `python.exe` in the user's
venv — eliminating the need for VC++ Redistributable system install (which requires
admin/UAC).

## Files

| DLL | Size | Required by |
|---|---:|---|
| `vcruntime140.dll`   | 124 KB | torch (c10.dll), most native Python extensions |
| `vcruntime140_1.dll` |  49 KB | torch (c10.dll) — needed for C++ exception handling |
| `msvcp140.dll`       | 545 KB | torch, numpy, opencv, anything using C++ STL |
| `concrt140.dll`      | 317 KB | Microsoft Concurrency Runtime — ffmpeg + some deps |
| `vccorlib140.dll`    | 344 KB | C++/CX runtime — required by some Microsoft components |

Total: ~1.4 MB

## Why bundled?

PyTorch's `c10.dll` and `torch_cpu.dll` link against MSVC C++ runtime. On a fresh
Windows 10/11 install these DLLs are NOT present — only systems that have ever
installed Visual Studio, a game using VC++, or the standalone redistributable
have them in `C:\Windows\System32\`.

Without these DLLs, every `import torch` crashes with:

    OSError: [WinError 1114] DLL initialization routine failed.
    Error loading "...\torch\lib\c10.dll" or one of its dependencies.

## Why next-to-python instead of system install?

Microsoft's `vc_redist.x64.exe` requires admin (UAC prompt). For non-technical
users in corporate/locked-down environments, UAC = blocker. Side-by-side
distribution (DLLs in same folder as the exe loading them) is fully supported by
Microsoft and the PE loader's DLL search order:

1. Directory containing the .exe          ← we put them here (venv/Scripts/)
2. System directory (System32)
3. Windows directory
4. Current working directory
5. PATH

So `pythonw.exe` in `venv\Scripts\` finds OUR copy first. No admin needed. No
SmartScreen warnings from running `vc_redist.x64.exe`.

## Microsoft license

These DLLs are part of the Microsoft Visual C++ Redistributable, which Microsoft
explicitly licenses for redistribution alongside applications. See:

  https://learn.microsoft.com/en-us/visualstudio/productinfo/vs2022-redistribution-vs

Specifically, the redistributable license file (`Redist.txt`) lists these DLLs
under the "Redist" category — meaning they may be distributed alongside an
application that uses them.

## How the bat uses them

After `uv venv` creates the venv, the bat does:

```bat
copy "%SRC%\runtime\vc\*.dll" "%VENV%\Scripts\" >nul
```

That's it. No registry edits, no UAC, no installer to run. Just file copy.
