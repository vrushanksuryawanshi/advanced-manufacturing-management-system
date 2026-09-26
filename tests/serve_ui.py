"""Start an isolated browser-test server. Never touches the user's demo database."""
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, seed_demo
from waitress import serve

Path('tmp/tests').mkdir(parents=True,exist_ok=True)
app=create_app(Path('tmp/tests')/('ui-'+secrets.token_hex(8)+'.db'),True)
seed_demo(app)
serve(app,host='127.0.0.1',port=5001)
