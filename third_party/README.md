# Bundled WebSocket runtime

`websockets-14.2-py3-none-any.whl` is the unmodified pure Python wheel from PyPI.
It provides the maintained Unix WebSocket transport for the Codex control socket.
The plugin loads it locally; init never installs Python packages or accesses PyPI.
The wheel includes the upstream BSD license at `websockets-14.2.dist-info/LICENSE`.

Maintainer download command:

```sh
python3 -m pip download --no-deps --only-binary=:all: --platform any --python-version 311 websockets==14.2 --dest third_party
```

The plugin package manifest records the wheel's SHA-256 alongside all other files.
