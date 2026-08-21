## sony-guid-setter

Small utility (fork of [falk0069/sony-pm-alt](https://github.com/falk0069/sony-pm-alt)) to set the GUID for Sony cameras. This version adds auto-detection of USB devices whose product string starts with "Sony".

### Quick start

For arm64 Mac:
```bash
sudo ./sony-guid-setter -a
```

For Intel Mac:
```bash
sudo ./sony-guid-setter-x64 -a
```

For Windows:
```bash
./sony-guid-setter.exe -a
```

### Manual device selection

Use `lsusb` to locate your camera, for example:

```text
Bus 002 Device 001: ID 054c:0c02 Sony Corporation ILCE-7M3
```

Then run:

```bash
sudo ./sony-guid-setter -g 054c:0c02
```

### Step-by-step
1. Connect your camera and wait until it shows as Mass Storage.
2. Run `sudo ./sony-guid-setter -a`.
3. When you see `Closing device...`, disconnect the camera.
4. On the camera, use "Send to Computer"; you should see `linux` displayed.

### Build

Prebuilt binary for macOS is included. To build yourself (requires `libusb-1.0`):

```bash
gcc sony-guid-setter.c -lusb-1.0 -o sony-guid-setter
```

### Reference

See the original repository for deeper details: [falk0069/sony-pm-alt](https://github.com/falk0069/sony-pm-alt)

### Huge courtesy of
- Pete Batard <pete@akeo.ie>
- Alan Stern
- Clemens Fruhwirth <clemens@endorphin.org>