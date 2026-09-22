"""
macOS Native Push Notifications Integration for LatticeShadow.

Provides a layered, non-blocking notification delivery logic:
- Layer 1: Cocoa NSUserNotificationCenter via PyObjC.
- Layer 2: AppleScript via `osascript` subprocess fallback.
- Layer 3: Warning log fallback for non-macOS/unsupported environments.

Ensures that notification dispatch runs asynchronously on a background daemon thread.
"""

import sys
import logging
import threading
import subprocess

logger = logging.getLogger("latticeshadow.notifications")

# Platform check
IS_MAC = sys.platform == "darwin"

# Track if PyObjC bindings are available and initialized
_has_objc = False
_NSUserNotification = None
_NSUserNotificationCenter = None
_NSDate = None
_NSRunLoop = None
_NotificationDelegateClass = None

if IS_MAC:
    try:
        import objc
        import Foundation
        
        # Override the bundle identifier dynamically if we're not running in a registered app context.
        # This prevents macOS from suppressing notifications from command-line scripts or background daemons.
        try:
            current_id = Foundation.NSBundle.mainBundle().bundleIdentifier()
            if not current_id or current_id.startswith("org.python."):
                class NSBundle(objc.Category(Foundation.NSBundle)):
                    def bundleIdentifier(self):
                        return "com.latticedb.shadow"
                logger.debug("Swizzled NSBundle bundleIdentifier to 'com.latticedb.shadow' for notification context.")
        except Exception as e:
            logger.debug("Failed to check or swizzle NSBundle: %s", e)

        # Declare the delegate to force notifications to show even if app is in the foreground
        class LatticeShadowNotificationDelegate(Foundation.NSObject):
            def userNotificationCenter_shouldPresentNotification_(self, center, notification):
                return True

        _NSUserNotification = Foundation.NSUserNotification
        _NSUserNotificationCenter = Foundation.NSUserNotificationCenter
        _NSDate = Foundation.NSDate
        _NSRunLoop = Foundation.NSRunLoop
        _NotificationDelegateClass = LatticeShadowNotificationDelegate
        _has_objc = True
    except Exception as e:
        logger.debug("Could not initialize PyObjC macOS notifications bindings: %s. Falling back to osascript.", e)


def _send_via_ns_user_notification(title: str, message: str) -> None:
    """
    Sends a notification banner using Cocoa's NSUserNotificationCenter.
    """
    if not _has_objc or _NSUserNotification is None or _NSUserNotificationCenter is None:
        raise RuntimeError("PyObjC or NSUserNotificationCenter is not available.")

    delegate = _NotificationDelegateClass.alloc().init()
    center = _NSUserNotificationCenter.defaultUserNotificationCenter()
    center.setDelegate_(delegate)

    notification = _NSUserNotification.alloc().init()
    notification.setTitle_(title)
    notification.setInformativeText_(message)
    notification.setSoundName_("NSUserNotificationDefaultSoundName")

    center.deliverNotification_(notification)

    # Briefly run the run loop on this thread to allow PyObjC to dispatch the event
    loop = _NSRunLoop.currentRunLoop()
    loop.runUntilDate_(_NSDate.dateWithTimeIntervalSinceNow_(0.15))


def _applescript_literal(value: str) -> str:
    """Encode notification text as one AppleScript string literal."""
    clean = "".join(char if char.isprintable() else " " for char in value)
    return '"' + clean.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _send_via_osascript(title: str, message: str) -> None:
    """
    Sends a notification banner using macOS osascript as a fallback.
    """
    cmd = ["osascript", "-e", f"display notification {_applescript_literal(message)} with title {_applescript_literal(title)}"]
    
    # Run synchronously within the background thread
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def _dispatch_notification(title: str, message: str) -> None:
    """
    Executes the notification dispatch. Tries Cocoa native APIs, falls back to osascript,
    and logs warnings if both fail.
    """
    if _has_objc:
        try:
            _send_via_ns_user_notification(title, message)
            logger.debug("Successfully delivered notification via NSUserNotificationCenter.")
            return
        except Exception as e:
            logger.debug("Failed to deliver notification via NSUserNotificationCenter: %s. Falling back to osascript.", e)

    # Fallback to AppleScript via osascript
    try:
        _send_via_osascript(title, message)
        logger.debug("Successfully delivered notification via osascript.")
    except Exception as e:
        logger.warning(
            "macOS notification failed. Fallback print: [%s] %s (Error: %s)",
            title, message, e
        )


def notify_drift(title: str, message: str) -> None:
    """
    Triggers a native desktop push notification on macOS.
    
    This function is thread-safe, non-blocking (runs on a background thread),
    and safely falls back to standard logging if PyObjC is missing or running on a non-macOS system.
    """
    if not IS_MAC:
        logger.warning("Notification triggered on non-macOS platform: [%s] %s", title, message)
        return

    # Run in a daemon thread so it is completely non-blocking to the main execution flow
    thread = threading.Thread(
        target=_dispatch_notification,
        args=(title, message),
        daemon=True,
        name="LatticeShadowNotificationThread"
    )
    thread.start()
