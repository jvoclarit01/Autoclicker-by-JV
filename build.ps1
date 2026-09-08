$ErrorActionPreference = "Stop"

python -m PyInstaller --noconfirm --clean --onefile --windowed --name autoclicker autoclicker.py
Copy-Item -LiteralPath ".\dist\autoclicker.exe" -Destination ".\autoclicker.exe" -Force
Get-FileHash -LiteralPath ".\autoclicker.exe" -Algorithm SHA256
