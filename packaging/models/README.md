# Bundled embedding models

Offline deployment: run `python packaging/models/fetch.py` once (online), then package the release — the app loads the snapshots fully offline. Model weights are gitignored; only the fetch scripts and this README are tracked.

- `fetch.py --gguf BAAI/bge-m3` pulls the bge-m3 GGUF (~700MB, hf-mirror by default) into `./gguf/`.
- `fetch_llama_bin.py` pulls the llama.cpp `llama-server` binary (+ shared libs) into `./llama-bin/` (`--platform windows` for `llama-server.exe`).

Docker images run both at build time and bake the artifacts into `/opt/proseforge/models` (disable with `--build-arg BUNDLE_EMBEDDINGS=0`); the runtime lookup prefers that bundled path over the download cache — see `BUNDLED_MODELS_ROOTS` in `proseforge/infrastructure/embeddings/llama_server.py`. Native bundles include `./gguf/` and `./llama-bin/` automatically when present on the build host (`packaging/native_bundle.py`).
