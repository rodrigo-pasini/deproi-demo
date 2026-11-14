"""Deproi example 'migration': records the schema version in the shared volume.

Runs as the migrate hook, with the NEW image, before the services are recreated.
"""

import os
import pathlib

SCHEMA = "3"

data = pathlib.Path(os.environ.get("APP_DATA", "/data"))
data.mkdir(parents=True, exist_ok=True)
(data / "schema_version").write_text(SCHEMA + "\n")
print(f"schema at version {SCHEMA}")
