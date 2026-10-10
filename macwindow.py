"""Mac window dressing for the pywebview window: hidden title bar, app name, appearance.

pywebview opens a plain titled window. These touches make it sit like a native app:
the page runs to the top edge with the traffic lights set into the sidebar, and the
page's own toolbar moves the window. Only imported when the app opens a window.
"""
import AppKit

# The page posts "drag" or "zoom" to window.webkit.messageHandlers[BRIDGE]. Its presence
# is also how the page knows it is inside the app window rather than a browser tab.
BRIDGE = "segueWindow"


class TitleBarBridge(AppKit.NSObject):
    """Lets the page's toolbar stand in for the title bar: drag to move, double-click to zoom."""

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        window = message.webView().window()
        if message.body() == "zoom":
            window.performZoom_(None)
            return
        # WKWebView swallows clicks in the title bar area, so start the drag ourselves
        # with the mouse-down that is still in flight.
        event = AppKit.NSApp.currentEvent()
        if event is None or event.type() not in (AppKit.NSEventTypeLeftMouseDown,
                                                 AppKit.NSEventTypeLeftMouseDragged):
            # Another event (often the window activating) arrived first. If the button
            # is still held, stand in a mouse-down where the pointer is now.
            if not AppKit.NSEvent.pressedMouseButtons() & 1:
                return
            event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                AppKit.NSEventTypeLeftMouseDown, window.mouseLocationOutsideOfEventStream(), 0,
                AppKit.NSProcessInfo.processInfo().systemUptime(), window.windowNumber(), None, 0, 1, 1.0)
        window.performWindowDragWithEvent_(event)


_bridge = TitleBarBridge.alloc().init()


def set_identity(name, version):
    """Name the app in the menu bar, Dock and About panel; a bare Python process says "Python".

    macOS reads the name when the application object is created, so call this before
    pywebview creates a window.
    """
    bundle = AppKit.NSBundle.mainBundle()
    info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
    info["CFBundleName"] = name
    info["CFBundleShortVersionString"] = version
    info["CFBundleVersion"] = version


def dark_mode():
    return AppKit.NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle") == "Dark"


def style(window):
    """Hide the title bar. Hook to the window's before_show event (main thread, not yet visible)."""
    native = window.native
    native.setTitlebarAppearsTransparent_(True)
    native.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    native.setStyleMask_(native.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
    for view in native.contentView().superview().subviews():
        if view.className() == "NSTitlebarContainerView":
            view.setBackgroundColor_(AppKit.NSColor.clearColor())  # pywebview paints it solid
    # An empty toolbar makes the title bar toolbar-height, which moves the traffic lights
    # down and in to where the page leaves room for them.
    native.setToolbar_(AppKit.NSToolbar.alloc().initWithIdentifier_("main"))
    native.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnified)
    # The web view only becomes the window's content view once the page has loaded,
    # so reach it through pywebview's own record of the window.
    from webview.platforms import cocoa
    webview = cocoa.BrowserView.instances[window.uid].webview
    webview.configuration().userContentController().addScriptMessageHandler_name_(_bridge, BRIDGE)
