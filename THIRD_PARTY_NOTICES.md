# Third-party notices

This distribution contains or interfaces with the following open-source projects.

## airMTP

- Project: https://github.com/shezi/airmtp
- Original project name: Airnef / airMTP
- License: GNU General Public License v3.0 or later
- Copyright: 2015, testcams.com

The source used for the bundled backend is included under
`source/vendor/airmtp`. The Windows SSDP socket level was changed from
`SOL_IP` to `IPPROTO_IP` so explicit multicast-interface selection works on
current Python/Windows builds.
Console-oriented Backspace progress output is sanitized by the graphical
frontend and is not written into the GUI log.

## ImagingEdge4Linux

- Project: https://github.com/schorschii/ImagingEdge4Linux
- License: GNU General Public License v3.0
- Copyright: Georg Sieber, 2024-2025

The direct-transfer implementation is a clean adaptation of the publicly documented SOAP/UPnP behavior and is distributed under GPL-3.0.

## sony-guid-setter protocol reference

- Project: https://github.com/FireFoxG/sony-guid-setter
- Upstream: https://github.com/falk0069/sony-pm-alt
- License: GNU Lesser General Public License v2.1 or later
- USB code derived from xusb.c, Copyright 2009-2012 Pete Batard
- Sony GUID protocol reverse engineering, Copyright 2016 Clemens Fruhwirth

The old libusb Windows executable is no longer distributed or executed. Its
source is retained under `source/vendor/sony-guid-setter` as the protocol and
license reference for the Sony GUID packet.

## Sony-PMCA-RE Windows MSC reference

- Project: https://github.com/ma1co/Sony-PMCA-RE
- License: MIT
- Copyright: the Sony-PMCA-RE contributors

The native pairing module independently implements the documented Windows
`IOCTL_SCSI_PASS_THROUGH_DIRECT` transport, following the target-validation
and standard `USBSTOR` approach demonstrated by Sony-PMCA-RE. No PMCA source
file or binary is bundled.
