import os
import time
import subprocess
import threading
try:
    import Quartz
    import Vision
    from Cocoa import NSURL, NSDictionary
except ImportError:
    Quartz = None
    Vision = None

from latticeshadow import config

class VisionAgent:
    def __init__(self):
        self.tmp_image_path = "/tmp/shadow_vision.png"
        
    def take_screenshot(self):
        """Take a silent screenshot using macOS native tools."""
        subprocess.run(["screencapture", "-x", self.tmp_image_path], check=True)
        return self.tmp_image_path
        
    def find_text_coordinates(self, target_text):
        """Use macOS Vision framework to find the coordinates of target_text on screen."""
        if not Vision:
            print("Vision framework not available. Ensure PyObjC is installed.")
            return None
            
        if not os.path.exists(self.tmp_image_path):
            return None
            
        url = NSURL.fileURLWithPath_(self.tmp_image_path)
        request_handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        
        success, error = request_handler.performRequests_error_([request], None)
        if not success:
            return None
            
        results = request.results()
        if not results:
            return None
            
        # Screen dimensions to convert bounding box to pixels
        # VNRecognizeTextRequest returns normalized coordinates (0.0 to 1.0)
        # Origin is bottom-left for Vision, but top-left for CGEvent
        main_display = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        width = Quartz.CGRectGetWidth(main_display)
        height = Quartz.CGRectGetHeight(main_display)
        
        for observation in results:
            top_candidates = observation.topCandidates_(1)
            if top_candidates:
                candidate = top_candidates[0]
                text = candidate.string()
                
                # Simple substring match
                if target_text.lower() in text.lower():
                    # Get bounding box
                    box = observation.boundingBox()
                    
                    # Convert to screen coordinates
                    x = box.origin.x * width
                    # Invert Y axis
                    y = height - (box.origin.y * height)
                    
                    # Target the center of the bounding box
                    w = box.size.width * width
                    h = box.size.height * height
                    
                    center_x = x + (w / 2)
                    center_y = y - (h / 2)
                    
                    return (center_x, center_y)
                    
        return None

    def click(self, x, y):
        """Simulate a mouse click at (x, y)."""
        if not Quartz:
            return
            
        # Check safety mode
        live_dangerously = config.get("automation.live_dangerously", False)
        if not live_dangerously:
            # Emulate "Confirm before click" by requiring user interaction
            # For automation purposes, we'll just log if we are not living dangerously
            # In a real GUI we'd show a prompt
            print(f"Safety Check: Would click at ({x}, {y}). Set automation.live_dangerously to True to bypass.")
            return

        # Create the mouse down and mouse up events
        point = Quartz.CGPoint(x, y)
        mouse_down = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft)
        mouse_up = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft)
            
        # Execute the click
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, mouse_down)
        time.sleep(0.05)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, mouse_up)

    def execute_replay(self, target_text):
        """End-to-end replay task."""
        self.take_screenshot()
        coords = self.find_text_coordinates(target_text)
        if coords:
            self.click(coords[0], coords[1])
            return True
        return False

# Initialize a default global config key if it doesn't exist
try:
    if config.get("automation.live_dangerously") is None:
        config.set("automation.live_dangerously", "False")
except Exception:
    pass
