#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo "  Balaji MF — Starting..."
echo "  Browser will open at http://localhost:5050"
echo "  Close this window to stop the server."
echo ""
pip install -q -r requirements.txt 2>/dev/null
python app.py
