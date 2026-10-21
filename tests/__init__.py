import os

# Tests must never make noise on a real machine.
os.environ.setdefault("PT_STUDIO_SOUND", "off")
