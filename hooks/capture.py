import os, sys
if os.environ.get("APPRENTICE_INTERNAL") == "1":
    try:
        n = 0
        while n < 1048576:
            chunk = os.read(0, 65536)
            if not chunk: break
            n += len(chunk)
    except BaseException: pass
    os._exit(0)
event = ""
try:
    import json
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.buffer.read(1048576 + 1)
    from apprentice import config
    home_dir = config.home()
    if os.environ.get("APPRENTICE_DISABLE") == "1" or os.path.exists(os.path.join(home_dir, "disabled")):
        os._exit(0)
    payload = None
    if not raw:
        config.log_error(event, "bad-stdin: empty")
    elif len(raw) > 1048576:
        config.log_error(event, "bad-stdin: over 1 MB")
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = None
            config.log_error(event, "bad-stdin: not a JSON object")
    if payload is not None:
        from apprentice import capture
        out = capture.handle(event, payload, os.environ)
        if out:
            sys.stdout.write(out)
            sys.stdout.flush()
except BaseException as e:
    try:
        from apprentice import config; config.log_error(event, "%s: %s" % (type(e).__name__, str(e)[:300]))
    except BaseException: pass
os._exit(0)
