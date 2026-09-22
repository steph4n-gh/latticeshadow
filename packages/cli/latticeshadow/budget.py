import os
import json
import datetime
from latticeshadow import config

BUDGET_FILE = os.path.expanduser("~/.latticeshadow/budget.json")

def _load_budget():
    if os.path.exists(BUDGET_FILE):
        try:
            with open(BUDGET_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "request_count": 0}

def _save_budget(data):
    os.makedirs(os.path.dirname(BUDGET_FILE), exist_ok=True)
    try:
        with open(BUDGET_FILE, "w") as f:
            json.dump(data, f)
        os.chmod(BUDGET_FILE, 0o600)
    except Exception:
        pass

def check_budget_and_increment() -> bool:
    """
    Checks if we have remaining API budget for today.
    If yes, increments the count and returns True.
    If no, returns False.
    """
    max_requests = config.get("automation.max_daily_llm_requests")
    if max_requests is None:
        max_requests = 50
    else:
        max_requests = int(max_requests)

    today = datetime.date.today().isoformat()
    data = _load_budget()

    if data.get("date") == today:
        if data.get("request_count", 0) >= max_requests:
            return False
        data["request_count"] = data.get("request_count", 0) + 1
    else:
        data["date"] = today
        data["request_count"] = 1

    _save_budget(data)
    return True

def get_today_requests() -> int:
    today = datetime.date.today().isoformat()
    data = _load_budget()
    if data.get("date") == today:
        return data.get("request_count", 0)
    return 0
