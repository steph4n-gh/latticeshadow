import sys
from unittest.mock import MagicMock, patch

try:
    import AppKit
except ImportError:
    class DummyNSObject:
        @classmethod
        def alloc(cls):
            return cls()
            
        def init(self):
            return self
            
        def __getattr__(self, name):
            return MagicMock()

    AppKit = MagicMock()
    AppKit.NSObject = DummyNSObject
    AppKit.NSPanel = DummyNSObject
    AppKit.NSTextField = DummyNSObject
    AppKit.NSTableView = DummyNSObject
    sys.modules["AppKit"] = AppKit
    sys.modules["PyObjCTools"] = MagicMock()

import pytest
from latticeshadow.menu import ShadowMenuApp, setup_spotlight, SearchResultsDataSource


def test_menu_app_initialization():
    # Make sure we don't crash when instantiating the objects
    delegate = ShadowMenuApp.alloc().init()
    assert delegate is not None
    
    # Verify SearchResultsDataSource works
    ds = SearchResultsDataSource.alloc().init()
    assert ds is not None
    assert ds.numberOfRowsInTableView_(None) == 0
    
    # Add some dummy results and verify
    ds.results = [("hello memory", 0.95)]
    assert ds.numberOfRowsInTableView_(None) == 1
    val = ds.tableView_objectValueForTableColumn_row_(None, None, 0)
    assert "[0.95]" in val
    assert "hello memory" in val


def test_setup_spotlight_headless():
    # Mock SpotlightWindow and other AppKit elements to avoid headless NSPanel server errors
    with patch("latticeshadow.menu.SpotlightWindow") as mock_win, \
         patch("latticeshadow.menu.AppKit.NSVisualEffectView") as mock_ve, \
         patch("latticeshadow.menu.SpotlightTextField") as mock_tf, \
         patch("latticeshadow.menu.AppKit.NSScrollView") as mock_sv, \
         patch("latticeshadow.menu.AppKit.NSTableView") as mock_tv, \
         patch("latticeshadow.menu.AppKit.NSTableColumn") as mock_tc, \
         patch("latticeshadow.menu.AppKit.NSTextFieldCell") as mock_tfc, \
         patch("latticeshadow.menu.GridMetalView") as mock_gmv:
         
        mock_win_instance = MagicMock()
        mock_win.alloc().initWithContentRect_styleMask_backing_defer_.return_value = mock_win_instance
        
        delegate = ShadowMenuApp.alloc().init()
        setup_spotlight(delegate)
        
        assert delegate.spotlight_window is not None
        assert delegate.spotlight_tf is not None


def test_setup_spotlight_layers_and_subviews():
    with patch("latticeshadow.menu.SpotlightWindow") as mock_win, \
         patch("latticeshadow.menu.AppKit.NSVisualEffectView") as mock_ve, \
         patch("latticeshadow.menu.SpotlightTextField") as mock_tf, \
         patch("latticeshadow.menu.AppKit.NSScrollView") as mock_sv, \
         patch("latticeshadow.menu.AppKit.NSTableView") as mock_tv, \
         patch("latticeshadow.menu.AppKit.NSTableColumn") as mock_tc, \
         patch("latticeshadow.menu.AppKit.NSTextFieldCell") as mock_tfc, \
         patch("latticeshadow.menu.GridMetalView") as mock_gmv:
         
        mock_win_instance = MagicMock()
        mock_win.alloc().initWithContentRect_styleMask_backing_defer_.return_value = mock_win_instance
        
        mock_ve_instance = MagicMock()
        mock_ve.alloc().initWithFrame_.return_value = mock_ve_instance
        
        mock_tf_instance = MagicMock()
        mock_tf.alloc().initWithFrame_.return_value = mock_tf_instance
        
        mock_sv_instance = MagicMock()
        mock_sv.alloc().initWithFrame_.return_value = mock_sv_instance
        
        mock_gmv_instance = MagicMock()
        mock_gmv.alloc().initWithFrame_.return_value = mock_gmv_instance
        
        delegate = ShadowMenuApp.alloc().init()
        setup_spotlight(delegate)
        
        # Verify setWantsLayer_ is called on all 4 components
        mock_ve_instance.setWantsLayer_.assert_any_call(True)
        mock_tf_instance.setWantsLayer_.assert_any_call(True)
        mock_sv_instance.setWantsLayer_.assert_any_call(True)
        mock_gmv_instance.setWantsLayer_.assert_any_call(True)
        
        # Verify GridMetalView is added at index 0 (NSWindowBelow)
        mock_ve_instance.addSubview_positioned_relativeTo_.assert_any_call(
            mock_gmv_instance, AppKit.NSWindowBelow, None
        )


def test_grid_metal_view_creation_and_safe_fallback():
    from latticeshadow.menu import GridMetalView
    rect = AppKit.NSMakeRect(0, 0, 100, 100)
    # The view should instantiate without raising an error
    view = GridMetalView.alloc().initWithFrame_(rect)
    assert view is not None
    
    # Check drawRect_ does not crash regardless of Metal availability
    try:
        view.drawRect_(rect)
    except Exception as e:
        pytest.fail(f"drawRect_ raised an exception: {e}")


def test_execute_recall():
    delegate = ShadowMenuApp.alloc().init()
    def exists_side_effect(path):
        if "holographic_today.bin" in path:
            return True
        return False

    with patch("os.path.exists", side_effect=exists_side_effect), \
         patch("latticeshadow.menu.get_vault") as mock_get_vault, \
         patch("latticeshadow.holographic_index.HolographicIndex") as mock_holographic_index, \
         patch.object(delegate, "notify") as mock_notify, \
         patch.object(delegate, "copyToClipboardAndNotify_score_") as mock_copy_clip:
         
        mock_vault = MagicMock()
        mock_get_vault.return_value = mock_vault
        mock_vault._embedder.embed.return_value = [0.1, 0.2, 0.3]
        
        mock_index = MagicMock()
        mock_holographic_index.load.return_value = mock_index
        mock_index.recall.return_value = ("test result", 0.99)
        
        delegate.executeRecall("test query")
        
        mock_get_vault.assert_called_once()
        mock_vault._embedder.embed.assert_called_once_with("test query")
        mock_holographic_index.load.assert_called_once()
        mock_index.recall.assert_called_once_with([0.1, 0.2, 0.3])
        mock_copy_clip.assert_called_once_with("test result", 0.99)


