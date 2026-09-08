# AutoClicker Pro

AutoClicker Pro is a desktop mouse automation tool built with Python, Tkinter, and pynput.

## Features

- Configurable click interval in hours, minutes, seconds, and milliseconds
- Left, middle, and right mouse buttons
- Single-click, double-click, and configurable click-and-hold actions
- Infinite, fixed-count, and fixed-duration repeat modes
- Current cursor or fixed X/Y position
- Global F6 start/stop hotkey
- Thread-safe activity log and responsive cancellation

In **Hold** mode, the selected button remains pressed for 1–10,000 milliseconds. After release, the app waits for the normal click interval before beginning the next action. Stopping the app or reaching a duration deadline releases the button immediately.

## Requirements

- Python 3.10 or newer
- Tkinter (normally included with Python)
- Dependencies listed in `requirements.txt`

## Installation

```powershell
python -m pip install -r requirements.txt
python autoclicker.py
```

On macOS, grant Accessibility permission to Python or the terminal running the app. Linux users need an active supported desktop session. Windows users normally do not need administrator privileges.

## Usage

1. Set the interval between completed mouse actions.
2. Select a mouse button and action type.
3. For Hold mode, set the hold duration in milliseconds; `500` means half a second and `1000` means one second.
4. Select a repeat mode and click position.
5. Click **Start** or press **F6**. Use the same control to stop.

The app waits 300 milliseconds before the first action so you can move the pointer away from the Start button. A fixed run duration begins after this startup grace period. An action interrupted while holding is released safely and is not counted as completed.

## Windows Executable

`autoclicker.exe` is a standalone Windows build of the same source. It is not digitally signed, so Windows may display a security warning. Verify the SHA-256 checksum published with the build if the file was downloaded from elsewhere.

Current v1.1 SHA-256: `4D9D9C238DEF8EA45CCA73223FFE159D66E639ABA2D95752481EDFEFA3581B1F`

To rebuild it locally:

```powershell
python -m pip install -r requirements-build.txt
.\build.ps1
```

The build script creates a one-file, windowed PyInstaller executable, copies it to the repository root, and prints its SHA-256 checksum.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The worker tests use a fake mouse controller and do not click the real mouse.

## Troubleshooting

- **Invalid setting:** Read the Activity Log for the exact accepted range.
- **Hotkey unavailable:** Another app may have captured F6, or the operating system may not permit global keyboard monitoring.
- **Mouse control fails:** Confirm pynput is installed and the operating system has granted the necessary input-control permission.
- **macOS/Linux:** Accessibility or desktop-session restrictions can prevent pynput from controlling input.

## Author

JV Oclarit

## License

MIT License. See `LICENSE`.
