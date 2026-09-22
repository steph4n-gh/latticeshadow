class ZkBridgeError(Exception):
    """Base exception class for all latticeshadow_db errors."""
    pass

class CalibrationError(ZkBridgeError):
    """Raised when calibration data is mismatched, corrupt, or invalid."""
    pass

class ConfigurationError(ZkBridgeError):
    """Raised when the bridge or adapter configuration is invalid."""
    pass
