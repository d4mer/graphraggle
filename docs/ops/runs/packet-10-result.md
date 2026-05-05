# Packet 10 Result

## Status: accepted

## Files Extracted

| File | Source Location | Destination |
|------|-----------------|-------------|
| `app/__init__.py` | install_rag_stack.sh line 239 (empty string) | `app/__init__.py` |
| `app/config.py` | install_rag_stack.sh lines 240-261 | `app/config.py` |
| `app/auth.py` | install_rag_stack.sh lines 294-304 | `app/auth.py` |
| `app/lightrag_client.py` | install_rag_stack.sh lines 305-348 | `app/lightrag_client.py` |
| `app/generation.py` | install_rag_stack.sh lines 887-940 | `app/generation.py` |

## Install Script Modified

Changed 5 HEREDOC entries from embedded source to `None` in the `files` dict, then added post-loop copy logic:
```python
import shutil
src_app = Path(__file__).parent.parent / "app"
dst_app = project_dir / "app"
for name in ["__init__.py", "config.py", "auth.py", "lightrag_client.py", "generation.py"]:
    src = src_app / name
    if src.exists():
        shutil.copy2(src, dst_app / name)
```

## Behavior Preserved

- Install script still generates all required files
- 5 app files now sourced from canonical `app/` directory
- Remaining HEREDOC entries (models.py, state_store.py, validation.py, api.py, worker.py) continue to embed source for now

## Verification

All 10 app files now exist as first-class files in `app/`:
`__init__.py`, `api.py`, `auth.py`, `config.py`, `generation.py`, `lightrag_client.py`, `models.py`, `state_store.py`, `validation.py`, `worker.py`

## Acceptance Criteria Met

1. App logic is no longer primarily embedded in installer for these 5 files.
2. Installer remains usable.
3. Behavior preserved.
4. Future changes can target real source files directly.