import time
import threading

def show_tooltip(text, duration=5.0):
    """Shows a native floating tooltip near the top right of the screen without stealing focus."""
    def _run():
        try:
            import tkinter as tk
        except ImportError:
            print(f"[Tooltip Fallback] {text}")
            return
            
        try:
            root = tk.Tk()
            root.overrideredirect(True)
            root.wm_attributes("-topmost", True)
            root.wm_attributes("-alpha", 0.9)
            # Use a translucent dark background
            root.configure(bg="#2d2d2d")
        except Exception as e:
            print(f"[Tooltip Fallback] {text} (Error: {e})")
            return
        
        # Position near top right
        screen_width = root.winfo_screenwidth()
        x = screen_width - 350
        y = 50
        root.geometry(f"+{x}+{y}")
        
        label = tk.Label(root, text=text, fg="#ffffff", bg="#2d2d2d", font=("Inter", 12), padx=15, pady=10)
        label.pack()
        
        # Auto-destroy after duration
        root.after(int(duration * 1000), root.destroy)
        
        # Update once, then enter loop
        root.update_idletasks()
        root.mainloop()

    # Run in a separate thread so it doesn't block the daemon
    t = threading.Thread(target=_run, daemon=True)
    t.start()

if __name__ == "__main__":
    show_tooltip("💡 You solved this 3 weeks ago in auth.py. Press Ctrl+G to apply the fix.")
    time.sleep(6)
