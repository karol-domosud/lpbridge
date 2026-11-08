# Third-party software

lpbridge is a thin macOS front end over other people's work. Almost everything
that makes a printer print here was written by the projects below. Their
licences and copyright notices travel with the software: inside the engine
image under `/usr/share/doc/<package>/copyright`, and inside the app bundle
next to the binaries. [SOURCES.md](../SOURCES.md) says how to obtain the
corresponding source for anything we distribute in compiled form.

## The print stack, in the Linux engine

| Project | What it does here | Licence |
|---|---|---|
| [CUPS](https://openprinting.github.io/cups/) | The engine *is* cupsd: driver matching (`cups-driverd`), queues, the filter chain, the network backends | Apache-2.0 |
| [cups-filters](https://github.com/OpenPrinting/cups-filters) | PDF → raster, the driverless (IPP Everywhere) path, generic PPDs | GPL-2.0 / LGPL-2.0 |
| [Ghostscript](https://www.ghostscript.com/) | Rasterizes for most foomatic drivers; `gs` device back ends for dozens of printer families | AGPL-3.0 |
| [OpenPrinting foomatic](https://openprinting.github.io/foomatic/) + `openprinting-ppds` | The model database and most of the PPDs — the reason a 1998 printer is recognised at all | GPL-2.0+ |
| [Gutenprint](https://gimp-print.sourceforge.io/) | Epson, Canon, HP, Lexmark, dye-sub drivers | GPL-2.0+ |
| [HPLIP](https://developers.hp.com/hp-linux-imaging-and-printing) (`hpcups`, `hpijs`) | HP LaserJet / DeskJet / OfficeJet | BSD-3-clause, MIT, GPL-2.0+ |
| [Epson ESC/P-R](https://www.openprinting.org/driver/epson-escpr/) | Modern Epson inkjets, including the EcoTank line | GPL-2.0+ |
| [foo2zjs](http://foo2zjs.rkkda.com/) | ZjStream / GDI "winprinters" | GPL-2.0+ |
| [SpliX](https://gitlab.com/ArchiveOS/splix) | Samsung / Xerox / Dell SPL and QPDL lasers | GPL-2.0+ |
| [brlaser](https://github.com/pdewacht/brlaser) | Brother mono lasers | GPL-2.0+ |
| ptouch, pnm2ppa, pxljr, min12xxw, m2300w, c2esp, c2050, cjet, sag-gdi, oki, dymo, fujixerox | The long tail of host-based printers | GPL-2.0+ (each) |
| [Ubuntu](https://ubuntu.com/) 24.04 LTS | The base system everything above is packaged and built by | many; `main` and `universe` only, all free software |
| [OpenSSH](https://www.openssh.com/) (server, in the engine) | The engine shell for hand-run driver installers | BSD |
| [FEX](https://fex-emu.com/) (`FEX`, `FEXServer`; the project's PPA build) | Runs x86-64 vendor filters on Apple Silicon | MIT |
| [QEMU](https://www.qemu.org/) (`qemu-i386-static`, `qemu-x86_64-static`) | Runs 32-bit x86 vendor filters; the x86-64 fallback where FEX cannot run | GPL-2.0 |

"Ubuntu" is a trademark of Canonical Ltd. lpbridge is built *on* Ubuntu and
is not an Ubuntu or Canonical product, nor endorsed by them.

## In the macOS app

| Project | What it does here | Licence |
|---|---|---|
| [vfkit](https://github.com/crc-org/vfkit) | Boots the engine on Apple's Virtualization.framework | Apache-2.0 |
| [CPython](https://www.python.org/), via [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Runs the agent without asking the user to install anything | PSF-2.0 (plus the licences of its bundled libraries, retained in the `python` directory) |

### The app icon

The app icon is ["Print"](https://www.svgrepo.com/svg/331557/print) by
[Jo Szczepanska](https://www.svgrepo.com/author/Jo%20Szczepanska/), from
svgrepo.com, under the
[Creative Commons Attribution-NonCommercial 4.0](https://creativecommons.org/licenses/by-nc/4.0/)
licence, recoloured to lpbridge's palette (`packaging/assets/AppIcon.svg`;
the original is `AppIcon-source.svg`). **It is licensed separately from the
code.** lpbridge itself
is GPLv3, which permits commercial redistribution; the icon's NC term does
not. Anyone redistributing lpbridge commercially, or packaging it for a
distribution that requires free licences throughout, must replace the icon.
No Apple system-provided images (SF Symbols) are used in the icon; Apple's
SDK licence forbids that.

The exact versions and checksums are in
[`dependencies.json`](dependencies.json).

## Used but not distributed

**Vendor printer drivers** (Canon UFR II and CAPT, Brother `*lpr` /
`*cupswrapper`, anything you add yourself) are downloaded from the vendor by
your own machine at first use, verified against a SHA-256 in
`agent/data/vendor-drivers.json`, and installed into your engine. They are
not bundled, and their own licence terms apply to them.
