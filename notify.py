"""
Windows desktop notification — a tiny, dependency-free toast.

Used by the headless/scheduled mode to surface "N new jobs today" without the
user watching the console. Implemented via a short PowerShell snippet that
shows a balloon tip through the standard Windows Forms NotifyIcon, so there is
NOTHING to pip-install and no third-party module to keep working.

Best-effort by design: notifying is never allowed to fail a run. On non-Windows
platforms, or if PowerShell is unavailable, it silently does nothing and
returns False.
"""

import subprocess
import sys


def _escape(text):
    """Make a string safe to embed inside a single-quoted PowerShell literal."""
    return str(text).replace("'", "''")


def notify(title, message):
    """
    Show a Windows balloon notification. Returns True if the command was
    dispatched without error, False otherwise. Never raises.
    """
    if not sys.platform.startswith("win"):
        return False

    title = _escape(title)
    message = _escape(message)
    ps = (
        "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = '" + title + "';"
        "$n.BalloonTipText = '" + message + "';"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(10000);"
        "Start-Sleep -Seconds 6;"
        "$n.Dispose();"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=30,
            capture_output=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
