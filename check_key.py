"""Run once: python check_key.py  -> confirms your Gemini key and model work."""
import reader
try:
    print("Key works. Model replied:", reader.ask([{"text": 'Reply with JSON {"ok": true}'}]).strip(), "| model:", reader.MODEL)
except reader.ReadError as e:
    print("PROBLEM:", e)
