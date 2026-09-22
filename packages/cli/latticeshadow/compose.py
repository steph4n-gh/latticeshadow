import json
import sqlite3
import os
from typing import List, Dict
from latticeshadow.config import get_data_dir
from latticeshadow.llm import ShadowLLM
from latticeshadow.sensitivity import classify, redact

class NeuralComposer:
    def __init__(self, db_path: str = None, llm=None):
        self.db_path = db_path or os.path.join(get_data_dir(), "shadow.sqlite")
        self.llm = llm if llm is not None else ShadowLLM.from_config()

    def _get_recent_history(self, limit: int = 10) -> List[Dict[str, str]]:
        if not os.path.exists(self.db_path):
            return []
            
        history = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT metadata_json, document 
                    FROM vectors 
                    WHERE collection = 'clipboard' 
                    ORDER BY rowid DESC 
                    LIMIT ?
                    """,
                    (limit * 2,)  # Fetch more to ensure we get enough terminal and clipboard events
                )
                rows = cursor.fetchall()
                for row in reversed(rows): # Reverse so they are in chronological order
                    meta_json, document = row
                    try:
                        meta = json.loads(meta_json)
                    except Exception:
                        continue
                        
                    source = meta.get("source")
                    if source in ("terminal", "clipboard"):
                        history.append({"source": source, "content": document})
        except Exception as e:
            print(f"Error reading history for compose: {e}")
            
        # Return only the last 'limit' items
        return history[-limit:]

    def predict_next_command(self) -> str:
        history = self._get_recent_history(limit=10)
        
        if not history or not self.llm:
            return ""
            
        prompt = (
            "You are a semantic shell autocomplete engine. Based on the user's recent clipboard copies and terminal command history, predict the EXACT next terminal command they want to run.\n"
            "Output ONLY the predicted command, no markdown, no explanation, no backticks.\n\n"
            "History of actions (chronological order):\n"
        )
        
        for item in history:
            source = item["source"].upper()
            content = item["content"].strip()
            sensitivity = classify(content)
            if sensitivity == "sensitive":
                content = "[SENSITIVE CONTENT OMITTED]"
            elif sensitivity == "unknown":
                content = redact(content)
            # truncate very long clipboard items for the prompt
            if len(content) > 500:
                content = content[:500] + "..."
            prompt += f"[{source}]: {content}\n"
            
        prompt += "\nPredict the next terminal command:"
        
        try:
            predicted = self.llm.complete(
                system="You are a semantic shell autocomplete engine.",
                user=prompt,
                temperature=0.2,
            ).strip()
            # Clean up if the LLM hallucinated backticks anyway
            if predicted.startswith("```"):
                lines = predicted.splitlines()
                if len(lines) >= 2:
                    predicted = lines[1].strip()
                predicted = predicted.replace("```", "").strip()
            return predicted
        except Exception as e:
            return f"Error: {e}"
