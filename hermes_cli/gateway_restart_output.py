"""Guarantee ``hermes gateway restart`` leaves a visible confirmation on the terminal.

Users intermittently saw a bare blank line instead of ``✓ Service restarted``: the success
paths do print, but a fast exit (the CLI reaped alongside the gateway it just restarted, or
block-buffered stdout behind a pipe) can drop the buffered line. The guard tracks whether
anything visible was written, prints a fallback confirmation on a silent success, and always
flushes before unwinding.
"""
import contextlib
import sys


def _restart_with_output_guard(args):
    """Run ``_cmd_restart`` and make sure a visible confirmation reaches stdout.

    Exceptions (SystemExit included) propagate unchanged and suppress the fallback: a failed
    restart is not a success.
    """
    from hermes_cli import gateway as gw

    stdout = sys.stdout
    orig_write = stdout.write
    visible = False

    def _tracked(s):
        nonlocal visible
        if isinstance(s, str) and s.strip():
            visible = True
        return orig_write(s)

    stdout.write = _tracked
    try:
        try:
            gw._cmd_restart(args)
        finally:
            stdout.write = orig_write
        if not visible:
            print("✓ Service restarted")
    finally:
        # A closed or broken stdout must not mask the restart's own exception.
        with contextlib.suppress(OSError, ValueError):
            stdout.flush()
