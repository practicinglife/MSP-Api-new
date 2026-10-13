"""MSP API Integration Tools application package."""

import os as _os

# Make .env values visible to os.environ for modules that read MSP_* settings directly (Docker's env_file
# already does this; a plain `uvicorn` run did not). Never overrides variables that are already set.
# Tests set MSP_SKIP_DOTENV=1 so a developer's .env can't leak into them.
if not _os.environ.get("MSP_SKIP_DOTENV"):
    try:
        from dotenv import load_dotenv as _load_dotenv

        _load_dotenv(override=False)
    except ImportError:  # python-dotenv is in requirements; tolerate its absence
        pass
