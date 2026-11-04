import os
import sys

# Ensure root and api directories are on sys.path
api_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(api_dir)

for p in [root_dir, api_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from main import app

# Export WSGI application for Vercel
handler = app
