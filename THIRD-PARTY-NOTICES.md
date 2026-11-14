# Third-party notices

The original application source is provided under the MIT license in `LICENSE`. Telegram is a third-party service; this application is not an official Telegram client or a product of Telethon's authors.

The Windows portable build includes Python 3.12.14, Tcl/Tk and the following runtime packages:

| Component | Version | License | Upstream |
|---|---|---|---|
| Telethon | 1.45.0 | MIT | https://github.com/LonamiWebs/Telethon |
| PySocks | 1.7.1 | BSD | https://github.com/Anorov/PySocks |
| cryptg | 0.6.0 | CC0 | https://github.com/cher-nov/cryptg |
| pyaes | 1.6.1 | MIT | https://github.com/ricmoo/pyaes |
| rsa | 4.9.1 | Apache 2.0 | https://github.com/sybrenstuvel/python-rsa |
| pyasn1 | 0.6.4 | BSD 2 clause | https://github.com/pyasn1/pyasn1 |
| Python | 3.12.14 | PSF and included component notices | https://www.python.org |
| Tcl/Tk | bundled with Python | Tcl/Tk license | https://www.tcl-lang.org |

The native build bootloader is supplied by PyInstaller 6.22.3 under its GPL license **with the distribution exception** described in `licenses/PyInstaller/COPYING.txt`. Build-time dependencies and their installed license texts are also included for attribution: pyinstaller-hooks-contrib 2026.9, altgraph 0.17.5, packaging 26.3, pefile 2024.8.26, and pywin32-ctypes 0.2.3. These build tools are not application runtime services.

License texts copied from the installed distributions are under `licenses/`, with versions recorded in `licenses/manifest.json`. Python's complete bundled notice is retained in `licenses/Python/LICENSE.txt`; Tk's notice is retained in `licenses/Tk/license.terms`. Copyright ownership remains with the original authors.

No source code from the original GPL Telegram Media Downloader userscript is embedded in this independent API client. The prior userscript remains a separate, unmodified delivery with its own license.
