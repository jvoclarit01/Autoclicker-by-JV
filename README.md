# Auto Clicker

A desktop auto clicker application built with Python, Tkinter, and pynput. Automates mouse clicks at specified intervals with customizable settings.

## Features

- **Click Interval**: Set precise timing in hours, minutes, seconds, and milliseconds
- **Button Selection**: Choose Left, Middle, or Right mouse button
- **Click Type**: Single or Double clicks
- **Repeat Modes**: Run until stopped or for a fixed number of clicks
- **Position Options**: Click at current cursor position or fixed X/Y coordinates
- **Hotkey Toggle**: Start/stop with F6 (or custom hotkey)
- **Activity Log**: Real-time logging of actions
- **Cross-Platform**: Works on Windows, macOS, and Linux

## Requirements

- Python 3.6+
- pynput library

## Installation

1. Install Python from [python.org](https://python.org) if not already installed
2. Install the required dependency:
   ```bash
   pip install pynput
   ```

## Usage

1. Run the application:
   ```bash
   python autoclicker.py
   ```

2. Configure settings:
   - Set click interval
   - Choose button and click type
   - Select repeat mode
   - Choose click position

3. Click "Start" or press F6 to begin clicking

4. Click "Stop" or press F6 again to stop

## Troubleshooting

- **Permission Issues**: On Windows, run as Administrator. On macOS, grant Accessibility permissions in System Preferences > Security & Privacy > Accessibility
- **Hotkey Not Working**: Ensure the application window is not minimized (hotkeys work globally)
- **Mouse Control Fails**: Check that pynput is installed correctly and no other applications are interfering with mouse input
- **Application Crashes**: Check the Activity Log for error messages

## Coder

JV Oclarit

## License

MIT License - feel free to use and modify as needed.