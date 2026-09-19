"""
run.py

Entry point for PiDjiRc2KmzSync. For development, this runs Flask's
built-in dev server. For production on the Pi, this isn't invoked
directly -- see pidjirc2kmzsync.service, which runs gunicorn instead
as a user-level systemd unit that starts automatically on boot:

    gunicorn -w 1 -b 0.0.0.0:8000 "flask_app.app:create_app()"

A single worker (-w 1) is intentional: only one process should hold the
MTP/USB session to the RC-2 at a time. The same reasoning applies to the
dev server below: Flask's debug reloader spawns a second process, which
would just as happily race the first one for the MTP session, so it's
disabled here.
"""

from flask_app.app import create_app

if __name__ == "__main__":
    app = create_app()
    # use_reloader=False: see module docstring -- a second reloader
    # process would compete for the RC-2's MTP/USB session.
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=False)
