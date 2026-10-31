"""WSGI entry point for production servers.

PythonAnywhere, gunicorn and similar hosts import this file and use the ``application`` object:

    gunicorn wsgi:application

Set the secret key (and, over HTTPS, the secure-cookie flag) in the server's environment first:
STOCKPILOT_SECRET_KEY and STOCKPILOT_COOKIE_SECURE=1. See DEPLOYMENT.md for step-by-step instructions.
"""

from app import create_app

application = create_app()
app = application  # some platforms look for this name instead
